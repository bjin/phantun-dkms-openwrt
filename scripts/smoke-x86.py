#!/usr/bin/env python3
"""QEMU install/load test of an x86/64 kmod-phantun APK on the stock OpenWrt image.

Exit status: 0 passed (or skipped: SNAPSHOT image and SDK from different buildbot runs),
anything else failed. Needs qemu-system-x86_64, openssl, gpgv and python3-pexpect.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (  # noqa: E402
    KERNEL_RE, Fatal, NotFound, fetch_verified_sums, http_get, log, target_bases,
)

PROMPT = r"root@[^:\r\n]*:[^#\r\n]*# "
TARGET = "x86/64"


def image_name(version: str) -> str:
    if version == "SNAPSHOT":
        return "openwrt-x86-64-generic-ext4-combined.img.gz"
    return f"openwrt-{version}-x86-64-generic-ext4-combined.img.gz"


def fetch_image(version: str, dest: Path) -> None:
    channel = "snapshot" if version == "SNAPSHOT" else "release"
    name = image_name(version)
    for base in target_bases(channel, version, TARGET):
        try:
            _, sums = fetch_verified_sums(base)
        except NotFound:
            continue
        break
    else:
        raise Fatal(f"no sha256sums for {version} {TARGET}")
    if name not in sums:
        raise Fatal(f"{name} not listed in {base}sha256sums")
    gz = dest.with_name(name)
    if gz.is_file() and hashlib.sha256(gz.read_bytes()).hexdigest() == sums[name]:
        log(f"reusing verified {gz}")
        data = gz.read_bytes()
    else:
        log(f"downloading {base}{name}")
        data = http_get(base + name, timeout=600)
        if hashlib.sha256(data).hexdigest() != sums[name]:
            raise Fatal(f"{name}: sha256 mismatch against verified sha256sums")
        gz.write_bytes(data)
    # OpenWrt appends metadata after the gzip stream; decompress the first member only.
    d = zlib.decompressobj(wbits=31)
    dest.write_bytes(d.decompress(data) + d.flush())
    if not d.eof:
        raise Fatal(f"{name}: truncated gzip stream")
    log(f"image {dest} ({dest.stat().st_size} bytes)")


def make_payload(apk: Path, apk_bin: str, tmp: Path) -> Path:
    key, pem = tmp / "test.key", tmp / "test.pem"
    subprocess.run(["openssl", "ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", str(key)],
                   check=True)
    subprocess.run(["openssl", "ec", "-in", str(key), "-pubout", "-out", str(pem)], check=True,
                   capture_output=True)
    repo = tmp / "repo"
    repo.mkdir()
    (repo / apk.name).write_bytes(apk.read_bytes())
    # Like publish.py: sign the APK itself, then index it.
    subprocess.run([apk_bin, "adbsign", "--allow-untrusted", "--sign-key", str(key), apk.name],
                   cwd=repo, check=True)
    subprocess.run([apk_bin, "mkndx", "--allow-untrusted", "--sign", str(key), "--output",
                    "packages.adb", apk.name], cwd=repo, check=True)
    payload = tmp / "payload.tar"
    with tarfile.open(payload, "w", format=tarfile.USTAR_FORMAT) as tf:
        tf.add(repo, arcname="repo")
        tf.add(pem, arcname="test.pem")
    size = payload.stat().st_size
    if size % 512:
        with open(payload, "ab") as f:
            f.write(b"\0" * (512 - size % 512))
    return payload


class Guest:
    def __init__(self, image: Path, payload: Path, logfile, kvm: bool) -> None:
        import pexpect

        self.pexpect = pexpect
        cmd = ["qemu-system-x86_64", "-m", "512", "-smp", "2", "-nographic", "-no-reboot",
               "-drive", f"file={image},format=raw,if=virtio",
               "-drive", f"file={payload},format=raw,if=virtio,readonly=on", "-nic", "none"]
        if kvm:
            cmd[1:1] = ["-enable-kvm", "-cpu", "host"]
        log("$ " + " ".join(cmd))
        self.p = pexpect.spawn(cmd[0], cmd[1:], encoding="utf-8", codec_errors="replace",
                               timeout=60, logfile=logfile)
        self.timeout_scale = 1 if kvm else 4

    def boot(self) -> None:
        self.p.expect("Please press Enter to activate this console.", timeout=240 * self.timeout_scale)
        self.p.sendline("")
        self.p.expect(PROMPT, timeout=30 * self.timeout_scale)

    def run(self, cmd: str, timeout: int = 60) -> tuple[int, str]:
        self.p.sendline(f"{cmd}; echo \"@RC=$?@\"")
        self.p.expect(r"@RC=(\d+)@", timeout=timeout * self.timeout_scale)
        out = str(self.p.before or "")
        match = self.p.match
        assert match is not None and hasattr(match, "group")
        rc = int(match.group(1))
        self.p.expect(PROMPT, timeout=30 * self.timeout_scale)
        # Drop the echoed command line.
        out = out.split("\n", 1)[1] if "\n" in out else ""
        return rc, out.replace("\r", "")

    def must(self, cmd: str, timeout: int = 60) -> str:
        rc, out = self.run(cmd, timeout)
        if rc != 0:
            raise Fatal(f"guest command failed (rc={rc}): {cmd}\n{out}")
        return out

    def poweroff(self) -> None:
        self.p.sendline("poweroff")
        self.p.expect(self.pexpect.EOF, timeout=60 * self.timeout_scale)


def smoke(args) -> str:
    m = KERNEL_RE.match(args.kernel)
    if not m:
        raise Fatal(f"bad --kernel {args.kernel!r}")
    linux_version, linux_release, vermagic = m.groups()
    want_kernel_pkg = f"kernel-{linux_version}~{vermagic}-r{linux_release}"
    apk = Path(args.apk).resolve()
    apk_bin = shutil.which(args.apk_bin)
    if not apk_bin:
        raise Fatal(f"apk binary {args.apk_bin!r} not found")
    apk_bin = os.path.abspath(apk_bin)
    work = Path(args.work)
    work.mkdir(parents=True, exist_ok=True)
    image = work / "openwrt.img"
    fetch_image(args.version, image)
    kvm = os.access("/dev/kvm", os.R_OK | os.W_OK)
    log(f"KVM: {'yes' if kvm else 'no (TCG, slow)'}")
    with tempfile.TemporaryDirectory(dir=work) as tmp, open(args.log, "w") as logfile:
        payload = make_payload(apk, apk_bin, Path(tmp))
        g = Guest(image, payload, logfile, kvm)
        try:
            g.boot()
            g.must("sleep 8")
            installed = g.must("apk list -I kernel").strip()
            log(f"guest kernel package: {installed}")
            if not re.search(rf"(^|\s){re.escape(want_kernel_pkg)}(\s|$)", installed):
                msg = f"image runs {installed!r}, the pin expects {want_kernel_pkg}"
                if args.version == "SNAPSHOT":
                    g.poweroff()
                    log(f"skipped (image/SDK mismatch): {msg}")
                    return "skipped"
                raise Fatal(msg)
            g.must("mkdir -p /tmp/p && tar -xf /dev/vdb -C /tmp/p")
            g.must("cp /tmp/p/test.pem /etc/apk/keys/")
            g.must("apk add --no-network --repository /tmp/p/repo/packages.adb kmod-phantun", timeout=120)
            g.must(f"test -f /lib/modules/{linux_version}/phantun.ko")
            before = int(g.must("dmesg | wc -l").strip().splitlines()[-1])
            g.must("insmod phantun managed_local_ports=51820")
            g.must("grep -q '^phantun ' /proc/modules")
            val = g.must("cat /sys/module/phantun/parameters/managed_local_ports").strip()
            if val.splitlines()[-1].strip() != "51820":
                raise Fatal(f"managed_local_ports reads back {val!r}")
            g.must("rmmod phantun")
            # A downloaded APK installs on its own signature (no index, no --allow-untrusted).
            g.must("apk del kmod-phantun", timeout=120)
            g.must(f"apk add --no-network /tmp/p/repo/{apk.name}", timeout=120)
            g.must(f"test -f /lib/modules/{linux_version}/phantun.ko")
            new = g.must(f"dmesg | tail -n +{before + 1}")
            log("guest dmesg since insmod:\n" + new)
            if bad := [l for l in new.splitlines() if re.search(r"BUG|Oops|WARNING|Call Trace", l)]:
                raise Fatal("kernel reported problems:\n" + "\n".join(bad))
            if "phantun" not in new:
                raise Fatal("no phantun messages in dmesg after insmod")
            g.poweroff()
        except g.pexpect.ExceptionPexpect as e:
            raise Fatal(f"console interaction failed: {type(e).__name__}; see {args.log}") from None
        finally:
            if g.p.isalive():
                g.p.terminate(force=True)
    return "passed"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--version", required=True, help="point release (25.12.6) or SNAPSHOT")
    ap.add_argument("--kernel", required=True, help="pin kernel identity <ver>-<rel>-<vermagic>")
    ap.add_argument("--apk", required=True, help="kmod-phantun APK to test")
    ap.add_argument("--apk-bin", required=True, help="apk v3 binary (e.g. <sdk>/staging_dir/host/bin/apk)")
    ap.add_argument("--work", required=True, help="scratch directory for the image")
    ap.add_argument("--log", required=True, help="serial console transcript")
    args = ap.parse_args()
    try:
        result = smoke(args)
    except Fatal as e:
        log(f"smoke test FAILED: {e}")
        return 1
    log(f"smoke test {result}")
    print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
