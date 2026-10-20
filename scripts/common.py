"""Shared helpers for matrix.py, publish.py and smoke-x86.py (Python 3 stdlib only)."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OPENWRT_KEY = REPO_ROOT / "keys" / "openwrt-8A8BC12F46B836C0F9CDB36F1D53D1877742E911.asc"
APK_PUBKEY = REPO_ROOT / "keys" / "phantun-openwrt.pem"
DOWNLOADS = "https://downloads.openwrt.org"
ARCHIVE = "https://archive.openwrt.org"

SCHEMA = 1
MIN_SERIES = (25, 12)
SNAPSHOT_RETAIN = 5
MAX_ITEMS = 256

TARGET_RE = re.compile(r"^[a-z0-9_-]+/[a-z0-9_-]+$")
SERIES_RE = re.compile(r"^\d{2}\.\d{2}$")
POINT_RE = re.compile(r"^\d{2}\.\d{2}\.\d+$")
KERNEL_RE = re.compile(r"^(\d+\.\d+\.\d+)-(\d+)-([0-9a-f]{32})$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
PHANTUN_VERSION_RE = re.compile(r"^\d+(\.\d+)*$")
SDK_LINE_RE = re.compile(r"^([0-9a-f]{64}) \*(openwrt-sdk-.*\.Linux-x86_64\.tar\.zst)$")
PIN_FIELDS = ("sdk", "sdk_sha256", "kernel", "arch", "version_code")


class Fatal(Exception):
    """Unrecoverable error; the command must fail."""


class NotFound(Exception):
    """HTTP 404."""


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def notice(msg: str) -> None:
    prefix = "::notice::" if os.environ.get("GITHUB_ACTIONS") == "true" else "notice: "
    print(prefix + msg, file=sys.stderr, flush=True)


def warning(msg: str) -> None:
    prefix = "::warning::" if os.environ.get("GITHUB_ACTIONS") == "true" else "warning: "
    print(prefix + msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- HTTP


def _request(url: str, method: str, headers: dict | None) -> urllib.request.Request:
    req = urllib.request.Request(url, method=method)
    req.add_header("User-Agent", "phantun-dkms-openwrt")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    return req


def http_get(url: str, *, headers: dict | None = None, method: str = "GET",
             attempts: int = 5, timeout: int = 60) -> bytes:
    """GET with retries/backoff. 404 raises NotFound at once; other 4xx raise Fatal at once."""
    delay = 2.0
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(_request(url, method, headers), timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise NotFound(url) from None
            if e.code < 500 and e.code != 429:
                raise Fatal(f"GET {url}: HTTP {e.code}") from None
            err = f"HTTP {e.code}"
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            err = str(e)
        if attempt == attempts:
            raise Fatal(f"{method} {url}: {err} (after {attempts} attempts)")
        log(f"{method} {url}: {err}; retry {attempt}/{attempts - 1} in {delay:.0f}s")
        time.sleep(delay)
        delay *= 2
    raise AssertionError("unreachable")


def http_exists(url: str) -> bool:
    try:
        http_get(url, method="HEAD")
        return True
    except NotFound:
        return False


def http_download(url: str, dest: Path, *, headers: dict | None = None) -> str:
    """Download to dest (retrying); returns sha256 hex."""
    data = http_get(url, headers=headers, timeout=600)
    dest.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------- versions


def version_key(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split("."))


def series_of(point: str) -> str:
    return ".".join(point.split(".")[:2])


def version_code_number(code: str) -> int:
    m = re.match(r"^r(\d+)", code)
    return int(m.group(1)) if m else -1


# --------------------------------------------------------------------------- GPG


def dearmor(armored: str) -> bytes:
    lines = armored.strip().splitlines()
    try:
        start = next(i for i, l in enumerate(lines) if l.startswith("-----BEGIN PGP PUBLIC KEY BLOCK"))
        end = next(i for i, l in enumerate(lines) if l.startswith("-----END PGP PUBLIC KEY BLOCK"))
    except StopIteration:
        raise Fatal("malformed armored key") from None
    body = lines[start + 1:end]
    while body and body[0].strip():  # armor headers
        if ":" not in body[0]:
            break
        body.pop(0)
    data = [l.strip() for l in body if l.strip() and not l.startswith("=")]
    return base64.b64decode("".join(data))


def gpg_verify(signature: bytes, data: bytes, what: str) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        keyring = Path(tmp, "openwrt.gpg")
        keyring.write_bytes(dearmor(OPENWRT_KEY.read_text()))
        sig = Path(tmp, "sig.asc")
        sig.write_bytes(signature)
        dat = Path(tmp, "data")
        dat.write_bytes(data)
        r = subprocess.run(["gpgv", "--keyring", str(keyring), str(sig), str(dat)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise Fatal(f"gpgv failed for {what}:\n{r.stderr}")


def parse_sha256sums(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        m = re.match(r"^([0-9a-f]{64}) \*(.+)$", line)
        if m:
            out[m.group(2)] = m.group(1)
    return out


# --------------------------------------------------------------------------- upstream


def target_bases(channel: str, version: str, target: str) -> list[str]:
    if channel == "release":
        return [f"{DOWNLOADS}/releases/{version}/targets/{target}/",
                f"{ARCHIVE}/releases/{version}/targets/{target}/"]
    return [f"{DOWNLOADS}/snapshots/targets/{target}/"]


def fetch_verified_sums(base: str) -> tuple[str, dict[str, str]]:
    sums_raw = http_get(base + "sha256sums")
    asc = http_get(base + "sha256sums.asc")
    gpg_verify(asc, sums_raw, base + "sha256sums")
    text = sums_raw.decode()
    return text, parse_sha256sums(text)


class Upstream:
    """Fetches pins from downloads.openwrt.org (archive.openwrt.org fallback), cached per process."""

    def __init__(self) -> None:
        self._pins: dict[tuple[str, str, str], dict | None] = {}

    def pin(self, channel: str, version: str, target: str) -> dict | None:
        key = (channel, version, target)
        if key not in self._pins:
            self._pins[key] = self._fetch_pin(channel, version, target)
        return copy.deepcopy(self._pins[key])

    def _fetch_pin(self, channel: str, version: str, target: str) -> dict | None:
        tries = 3 if channel == "snapshot" else 1
        for attempt in range(1, tries + 1):
            try:
                return self._fetch_pin_once(channel, version, target)
            except Inconsistent as e:
                if attempt == tries:
                    raise Fatal(str(e)) from None
                log(f"{e}; snapshot upload in progress? retry {attempt}/{tries - 1} in 60s")
                time.sleep(60)
        raise AssertionError("unreachable")

    def _fetch_pin_once(self, channel: str, version: str, target: str) -> dict | None:
        what = f"{version} {target}"
        for base in target_bases(channel, version, target):
            try:
                profiles_raw = http_get(base + "profiles.json")
                break
            except NotFound:
                continue
        else:
            log(f"{what}: profiles.json 404 -> target absent")
            return None
        _, sums = fetch_verified_sums(base)
        sdk_lines = [(h, n) for n, h in sums.items()
                     if SDK_LINE_RE.match(f"{h} *{n}") and "/" not in n]
        if len(sdk_lines) != 1:
            raise Fatal(f"{what}: expected exactly one SDK in {base}sha256sums, found {len(sdk_lines)}")
        sdk_sha, sdk = sdk_lines[0]
        got = hashlib.sha256(profiles_raw).hexdigest()
        profiles_signed = sums.get("profiles.json") == got
        if not profiles_signed and channel != "release":
            raise Inconsistent(f"{what}: profiles.json sha256 {got} does not match verified sha256sums")
        p = json.loads(profiles_raw)
        lk = p["linux_kernel"]
        kernel = f"{lk['version']}-{lk['release']}-{lk['vermagic']}"
        if not KERNEL_RE.match(kernel):
            raise Fatal(f"{what}: malformed kernel identity {kernel!r}")
        if p.get("target") != target:
            raise Fatal(f"{what}: profiles.json target is {p.get('target')!r}")
        want_version = version if channel == "release" else "SNAPSHOT"
        if p.get("version_number") != want_version:
            raise Inconsistent(f"{what}: profiles.json version_number is {p.get('version_number')!r}")
        arch, version_code = p.get("arch_packages"), p.get("version_code")
        if not arch or not version_code:
            raise Fatal(f"{what}: profiles.json lacks arch_packages/version_code")
        adb = f"kmods/{kernel}/packages.adb"
        if adb not in sums:
            raise Inconsistent(f"{what}: {adb} not listed in sha256sums")
        if not http_exists(base + adb):
            raise Inconsistent(f"{what}: {base}{adb} missing")
        if not profiles_signed:
            # Upstream rewrote a release's profiles.json without re-signing sha256sums (25.12.6
            # qualcommax/ipq50xx, 2026-10-20). Accept it only when the signed files confirm its
            # kernel identity and version_code; its arch is checked by build-one.sh against the
            # APK that the verified SDK builds.
            kmods = sorted({n.split("/")[1] for n in sums if re.fullmatch(r"kmods/[^/]+/packages\.adb", n)})
            buildinfo = http_get(base + "version.buildinfo")
            if sums.get("version.buildinfo") != hashlib.sha256(buildinfo).hexdigest():
                raise Fatal(f"{what}: neither profiles.json nor version.buildinfo match verified sha256sums")
            signed_code = buildinfo.decode().strip()
            if kmods != [kernel] or signed_code != version_code:
                raise Fatal(f"{what}: unsigned profiles.json ({kernel}, {version_code}) disagrees with "
                            f"signed sha256sums kmods {kmods} / version.buildinfo {signed_code!r}")
            notice(f"{what}: profiles.json sha256 {got} is not in the signed sha256sums; kernel and "
                   f"version_code confirmed by signed files, arch {arch} is checked by the build")
        pin = {"sdk": sdk, "sdk_sha256": sdk_sha, "kernel": kernel, "arch": arch,
               "version_code": version_code}
        log(f"{what}: {pin}")
        return pin


class Inconsistent(Exception):
    """Upstream files disagree (upload in progress)."""


def fetch_versions() -> dict:
    return json.loads(http_get(f"{DOWNLOADS}/.versions.json"))


def fetch_release_targets(version: str) -> set[str] | None:
    """Targets listed in the release's .targets.json; None if not (yet) published."""
    for host in (DOWNLOADS, ARCHIVE):
        try:
            return set(json.loads(http_get(f"{host}/releases/{version}/.targets.json")))
        except NotFound:
            continue
    return None


# --------------------------------------------------------------------------- matrix


def load_json(path: str | Path):
    with open(path) as f:
        return json.load(f)


def _ordered(d: dict, first: tuple[str, ...]) -> dict:
    out = {k: d[k] for k in first if k in d}
    for k in sorted(d):
        if k not in out:
            out[k] = d[k]
    return out


def canonical_pin(pin: dict | None) -> dict | None:
    return None if pin is None else _ordered(pin, PIN_FIELDS)


def canonical_matrix(m: dict) -> dict:
    out = _ordered(m, ("schema", "phantun", "policy", "targets", "snapshot_exclude",
                       "releases", "snapshot"))
    if isinstance(m.get("phantun"), dict):
        out["phantun"] = _ordered(m["phantun"], ("version", "tarball_sha256", "pkg_release", "hold"))
    if isinstance(m.get("policy"), dict):
        out["policy"] = _ordered(m["policy"], ("hold_openwrt", "build_point_releases",
                                               "retain_point_releases", "snapshot"))
    if isinstance(m.get("releases"), dict):
        rel = {}
        for s in sorted(m["releases"], key=_safe_version_key, reverse=True):
            points = m["releases"][s]
            if isinstance(points, dict):
                rel[s] = {}
                for p in sorted(points, key=_safe_version_key, reverse=True):
                    pins = points[p]
                    rel[s][p] = ({t: canonical_pin(pins[t]) if isinstance(pins[t], dict) else pins[t]
                                  for t in sorted(pins)} if isinstance(pins, dict) else pins)
            else:
                rel[s] = points
        out["releases"] = rel
    if isinstance(m.get("snapshot"), dict):
        out["snapshot"] = {t: canonical_pin(v) if isinstance(v, dict) else v
                           for t, v in sorted(m["snapshot"].items())}
    return out


def _safe_version_key(v: str) -> tuple:
    try:
        return (0, version_key(v))
    except ValueError:
        return (1, v)


def dump_matrix(m: dict) -> str:
    return json.dumps(canonical_matrix(m), indent=2) + "\n"


def pin_equal(a: dict | None, b: dict | None) -> bool:
    if a is None or b is None:
        return a is b
    return all(a.get(k) == b.get(k) for k in PIN_FIELDS)


# --------------------------------------------------------------------------- manifest / feed


def entry_key(e: dict) -> tuple:
    return (e["channel"], e["version"], e["target"], e["kernel"], e["phantun_version"],
            int(e["pkg_release"]))


def kmods_dir(channel: str, version: str, target: str, kernel: str) -> str:
    if channel == "release":
        return f"releases/{version}/targets/{target}/kmods/{kernel}"
    return f"snapshots/targets/{target}/kmods/{kernel}"


def apk_version(kernel: str, phantun_version: str, pkg_release: int) -> str:
    m = KERNEL_RE.match(kernel)
    if not m:
        raise Fatal(f"malformed kernel identity {kernel!r}")
    linux_version = m.group(1)
    return f"{linux_version}.{phantun_version}-r{pkg_release}"


def apk_path(channel: str, version: str, target: str, kernel: str, phantun_version: str,
             pkg_release: int) -> str:
    return (f"{kmods_dir(channel, version, target, kernel)}/"
            f"kmod-phantun-{apk_version(kernel, phantun_version, pkg_release)}.apk")


def retention(entries: list[dict], matrix: dict) -> tuple[list[dict], list[tuple[dict, str]]]:
    """Feed retention (DEVELOPMENT.md, "Retention"). Returns (kept, [(deleted entry, reason)])."""
    ph = (matrix["phantun"]["version"], int(matrix["phantun"]["pkg_release"]))
    targets = set(matrix["targets"])
    policy = matrix["policy"]
    kept: list[dict] = []
    deleted: list[tuple[dict, str]] = []

    releases = [e for e in entries if e["channel"] == "release"]
    snapshots = [e for e in entries if e["channel"] == "snapshot"]

    survivors = []
    for e in releases:
        if (e["phantun_version"], int(e["pkg_release"])) != ph:
            deleted.append((e, "phantun version/pkg_release differs from matrix"))
        elif e["series"] not in matrix["releases"]:
            deleted.append((e, f"series {e['series']} no longer tracked"))
        elif e["target"] not in targets:
            deleted.append((e, "target no longer listed"))
        else:
            survivors.append(e)
    by_series: dict[str, set[str]] = {}
    for e in survivors:
        by_series.setdefault(e["series"], set()).add(e["version"])
    keep_points = set()
    for points in by_series.values():
        newest = sorted(points, key=version_key, reverse=True)[:policy["retain_point_releases"]]
        keep_points.update(newest)
    for e in survivors:
        if e["version"] in keep_points:
            kept.append(e)
        else:
            deleted.append((e, f"point release {e['version']} beyond the newest "
                               f"{policy['retain_point_releases']} in the feed"))

    if not policy["snapshot"]:
        deleted.extend((e, "policy.snapshot is false") for e in snapshots)
    else:
        per_target: dict[str, list[dict]] = {}
        for e in snapshots:
            if e["target"] not in targets:
                deleted.append((e, "target no longer listed"))
            elif (e["phantun_version"], int(e["pkg_release"])) != ph:
                deleted.append((e, "phantun version/pkg_release differs from matrix"))
            else:
                per_target.setdefault(e["target"], []).append(e)
        for es in per_target.values():
            es.sort(key=lambda e: (version_code_number(e["version_code"]), e["kernel"]), reverse=True)
            kept.extend(es[:SNAPSHOT_RETAIN])
            deleted.extend((e, f"older than the newest {SNAPSHOT_RETAIN} snapshot identities")
                           for e in es[SNAPSHOT_RETAIN:])
    return kept, deleted


def sort_entries(entries: list[dict]) -> list[dict]:
    def key(e):
        return (e["channel"], _safe_version_key(e["version"]), e["target"],
                version_code_number(e["version_code"]), e["kernel"])
    return sorted(entries, key=key)


def write_github_output(pairs: dict[str, str]) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        raise Fatal("GITHUB_OUTPUT is not set")
    with open(path, "a") as f:
        for k, v in pairs.items():
            if "\n" in v:
                raise Fatal(f"multi-line output {k}")
            f.write(f"{k}={v}\n")
