#!/usr/bin/env python3
"""Assemble the gh-pages tree (DEVELOPMENT.md, "publish.py"): sign and add built APKs, apply
retention, re-index, write manifest.json, index.html, phantun-openwrt.pem and pages-state.json.
Never deploys or commits.

Artifacts: --artifacts holds one directory per build item, each with item.json (the plan item),
status (ok|stale|failed) and, when ok, build.json plus the APK (scripts/build-one.sh --out).
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (  # noqa: E402
    APK_PUBKEY, Fatal, apk_path, entry_key, load_json, log, retention, sha256_file, sort_entries,
)

ROOT_FILES = ("index.html", "phantun-openwrt.pem", "manifest.json", "pages-state.json")
PAGES_URL = "https://bjin.github.io/phantun-dkms-openwrt"
MAX_TREE_BYTES = 500 * 1024 * 1024
MANIFEST_SCHEMA = 2
ENTRY_FIELDS = ("channel", "series", "version", "target", "kernel", "arch", "version_code",
                "phantun_version", "pkg_release", "path", "apk_sha256", "apk_size", "unsigned_sha256",
                "built_at", "run_url")


def key_of_item(it: dict) -> tuple:
    return (it["channel"], it["version"], it["target"], it["kernel"], it["phantun_version"],
            int(it["pkg_release"]))


def load_artifacts(art_dir: Path) -> dict[str, dict]:
    """item id -> {"item", "status", "dir", "build"}

    actions/download-artifact puts each artifact into its own subdirectory, except when the pattern
    matches a single artifact: then its files land directly in art_dir.
    """
    out = {}
    for d in [art_dir, *sorted(p for p in art_dir.iterdir() if p.is_dir())]:
        item_file = d / "item.json"
        if not item_file.exists():
            if d != art_dir:
                log(f"ignoring {d}: no item.json")
            continue
        item = load_json(item_file)
        status_file = d / "status"
        status = status_file.read_text().strip() if status_file.exists() else "failed"
        build = load_json(d / "build.json") if (d / "build.json").exists() else None
        if item["id"] in out:
            raise Fatal(f"duplicate artifact for {item['id']}")
        out[item["id"]] = {"item": item, "status": status, "dir": d, "build": build}
    return out


def checked_apk(art: dict) -> tuple[Path, dict]:
    item, build, d = art["item"], art["build"], art["dir"]
    if build is None:
        raise Fatal(f"{item['id']}: status ok but no build.json")
    if key_of_item(build["item"]) != key_of_item(item):
        raise Fatal(f"{item['id']}: build.json item {build['item']} does not match plan item")
    for k in ("sdk_sha256", "arch", "version_code", "phantun_sha256"):
        if build["item"][k] != item[k]:
            raise Fatal(f"{item['id']}: build.json {k} {build['item'][k]} != plan {item[k]}")
    rel = apk_path(item["channel"], item["version"], item["target"], item["kernel"],
                   item["phantun_version"], int(item["pkg_release"]))
    name = rel.rsplit("/", 1)[1]
    if build["apk"] != name:
        raise Fatal(f"{item['id']}: APK {build['apk']} != expected {name}")
    apk = d / name
    if not apk.is_file():
        raise Fatal(f"{item['id']}: {apk} missing")
    if sha256_file(apk) != build["apk_sha256"]:
        raise Fatal(f"{item['id']}: {apk} sha256 does not match build.json")
    entry = {
        "channel": item["channel"], "series": item.get("series"), "version": item["version"],
        "target": item["target"], "kernel": item["kernel"], "arch": item["arch"],
        "version_code": item["version_code"], "phantun_version": item["phantun_version"],
        "pkg_release": int(item["pkg_release"]), "path": rel, "unsigned_sha256": build["apk_sha256"],
        "built_at": build["built_at"], "run_url": build["run_url"],
    }
    if entry["channel"] != "release":
        del entry["series"]
    return apk, entry


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if r.returncode != 0:
        raise Fatal(f"{' '.join(cmd)} failed ({r.returncode}):\n{r.stdout}{r.stderr}")
    return r


def verify_signature(apk_bin: str, path: Path, pubkey: Path) -> None:
    """`apk verify` an index or APK with pubkey as the only trusted key."""
    with tempfile.TemporaryDirectory() as root:
        keys = Path(root, "etc/apk/keys")
        keys.mkdir(parents=True)
        shutil.copy(pubkey, keys / "phantun-openwrt.pem")
        run([apk_bin, "--root", root, "verify", str(path.resolve())])


def sign_apk(apk_bin: str, src: Path, dest: Path, unsigned_sha256: str, signing_key: Path,
             pubkey: Path) -> None:
    """Write src to dest carrying exactly one signature, made with signing_key.

    `apk adbsign` exits 0 even when it fails (and may leave a truncated file), so the result is
    checked instead: it must verify with pubkey alone and, with its signatures removed, be
    byte-identical to the build output (unsigned_sha256)."""
    with tempfile.TemporaryDirectory() as tmp:
        signed, bare = Path(tmp, "signed.apk"), Path(tmp, "bare.apk")
        shutil.copyfile(src, signed)
        run([apk_bin, "adbsign", "--allow-untrusted", "--reset-signatures", "--sign-key",
             str(signing_key), str(signed)])
        verify_signature(apk_bin, signed, pubkey)
        shutil.copyfile(signed, bare)
        run([apk_bin, "adbsign", "--allow-untrusted", "--reset-signatures", str(bare)])
        if sha256_file(bare) != unsigned_sha256:
            raise Fatal(f"{dest}: signed APK minus signatures is not the build output {unsigned_sha256}")
        shutil.copyfile(signed, dest)


def index_dir(apk_bin: str, kdir: Path, signing_key: Path, pubkey: Path) -> None:
    apks = sorted(p.name for p in kdir.glob("*.apk"))
    adb = kdir / "packages.adb"
    adb.unlink(missing_ok=True)
    run([apk_bin, "mkndx", "--allow-untrusted", "--sign", str(signing_key), "--output",
         "packages.adb", *apks], cwd=kdir)
    verify_signature(apk_bin, adb, pubkey)
    log(f"indexed and verified {kdir}")


def tree_files(pages: Path) -> list[str]:
    return sorted(str(p.relative_to(pages)) for p in pages.rglob("*")
                  if p.is_file() and ".git" not in p.relative_to(pages).parts)


def content_sha256(pages: Path) -> str:
    h = hashlib.sha256()
    for rel in tree_files(pages):
        if rel == "pages-state.json":
            continue
        h.update(f"{rel}\0{sha256_file(pages / rel)}\n".encode())
    return h.hexdigest()


def remove_empty_dirs(pages: Path) -> None:
    for d in sorted((p for p in pages.rglob("*") if p.is_dir() and ".git" not in p.parts),
                    key=lambda p: len(p.parts), reverse=True):
        if d.name != ".git" and not any(d.iterdir()):
            d.rmdir()


def render_index(entries: list[dict], pem_sha256: str) -> str:
    def row(e):
        return ("<tr>" + "".join(f"<td>{html.escape(str(v))}</td>" for v in (
            e["version"], e["target"], e["kernel"], e["arch"], e["version_code"]))
            + f'<td><a href="{html.escape(e["path"])}">kmod-phantun {html.escape(e["path"].rsplit("/kmod-phantun-", 1)[1][:-4])}</a></td>'
            + f'<td><a href="{html.escape(e["path"].rsplit("/", 1)[0])}/packages.adb">packages.adb</a></td></tr>')
    rel = [e for e in entries if e["channel"] == "release"]
    snap = [e for e in entries if e["channel"] == "snapshot"]
    head = ("<tr><th>OpenWrt</th><th>Target</th><th>Kernel</th><th>Arch</th><th>Revision</th>"
            "<th>Package</th><th>Index</th></tr>")
    table = lambda es: (f"<table>{head}{''.join(row(e) for e in es)}</table>" if es  # noqa: E731
                        else "<p>None.</p>")
    setup = f"""BASE={PAGES_URL}
uclient-fetch -O /etc/apk/keys/phantun-openwrt.pem "$BASE/phantun-openwrt.pem"
sha256sum /etc/apk/keys/phantun-openwrt.pem   # must print {pem_sha256}
sed -nE "s#^.*/((releases|snapshots)/.*/kmods/[^/]+/packages\\.adb)\\$#$BASE/\\1#p" \\
    /etc/apk/repositories.d/distfeeds.list > /etc/apk/repositories.d/phantun.list
apk update && apk add kmod-phantun
insmod phantun managed_local_ports=51820   # parameters: see the phantun-dkms README"""
    phv = sorted({(e["phantun_version"], e["pkg_release"]) for e in entries})
    phs = ", ".join(f"{v}-r{r}" for v, r in phv) or "none"
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>kmod-phantun feeds for OpenWrt</title>
<style>
body {{ font-family: sans-serif; max-width: 72em; margin: 2em auto; padding: 0 1em; }}
pre {{ background: #f4f4f4; padding: 1em; overflow-x: auto; }}
table {{ border-collapse: collapse; }}
td, th {{ border: 1px solid #ccc; padding: 0.2em 0.5em; text-align: left; font-size: 90%; }}
</style>
</head>
<body>
<h1>kmod-phantun feeds for OpenWrt</h1>
<p>Signed apk feeds with the <a href="https://github.com/bjin/phantun-dkms">phantun-dkms</a> kernel
module for stock OpenWrt firmware (25.12 and later). Built by
<a href="https://github.com/bjin/phantun-dkms-openwrt">bjin/phantun-dkms-openwrt</a>.
phantun-dkms package version: {html.escape(phs)}.</p>
<h2>Router setup</h2>
<pre>{html.escape(setup)}</pre>
<p>The module only installs when the router runs exactly a (version, target, kernel) listed below.
After every firmware upgrade, regenerate <code>phantun.list</code> with the same command and
reinstall. Public key sha256: <code>{pem_sha256}</code>.</p>
<h2>Stable releases</h2>
{table(rel)}
<h2>SNAPSHOT</h2>
{table(snap)}
</body>
</html>
"""


def publish(args) -> bool:
    matrix = load_json(args.matrix)
    apk_bin = shutil.which(args.apk)
    if not apk_bin:
        raise Fatal(f"apk binary {args.apk!r} not found")
    apk_bin = str(Path(apk_bin).resolve())
    pages = Path(args.pages)
    pages.mkdir(parents=True, exist_ok=True)
    manifest_file = pages / "manifest.json"
    old_state = content_sha256(pages) if (pages / "pages-state.json").exists() else None
    old_manifest = load_json(manifest_file) if manifest_file.exists() else {"entries": []}
    entries = old_manifest["entries"]
    items = load_json(args.items) if args.items else []
    art_dir = Path(args.artifacts) if args.artifacts else None
    arts = load_artifacts(art_dir) if art_dir and art_dir.is_dir() else {}

    # 1. Release items are all-or-nothing.
    bad = [it["id"] for it in items
           if it["channel"] == "release" and arts.get(it["id"], {}).get("status") != "ok"]
    if bad:
        raise Fatal(f"release items not ok, refusing to publish: {bad}")
    for it in items:
        st = arts.get(it["id"], {}).get("status", "missing")
        if st != "ok":
            log(f"skipping {it['id']}: {st}")

    pubkey = Path(args.pubkey).resolve()
    pubkey_sha256 = sha256_file(pubkey)
    signing_key = Path(args.signing_key).resolve()
    pages_pem = pages / "phantun-openwrt.pem"
    # Key rotation, or a tree from before APKs were signed (schema 1 has no pubkey_sha256).
    resign = old_manifest.get("pubkey_sha256") != pubkey_sha256
    if resign and entries:
        log("published with another key or unsigned APKs: re-signing every APK and index")

    # 2. Sign and add built APKs.
    changed_dirs: set[str] = set()
    added: set[tuple] = set()
    by_key = {entry_key(e): e for e in entries}
    for it in items:
        art = arts.get(it["id"])
        if not art or art["status"] != "ok":
            continue
        apk, entry = checked_apk(art)
        dest = pages / entry["path"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        sign_apk(apk_bin, apk, dest, entry["unsigned_sha256"], signing_key, pubkey)
        entry.update(apk_sha256=sha256_file(dest), apk_size=dest.stat().st_size)
        by_key[entry_key(entry)] = entry
        added.add(entry_key(entry))
        changed_dirs.add(str(Path(entry["path"]).parent))
        log(f"added {entry['path']}")
    entries = list(by_key.values())

    # 3-4. Retention.
    kept, deleted = retention(entries, matrix)
    for e, why in deleted:
        (pages / e["path"]).unlink(missing_ok=True)
        changed_dirs.add(str(Path(e["path"]).parent))
        log(f"pruned {e['path']}: {why}")
    entries = sort_entries(kept)

    # 5. Re-sign the kept APKs after a key change.
    if resign:
        for e in entries:
            if entry_key(e) in added:
                continue
            f = pages / e["path"]
            if sha256_file(f) != e["apk_sha256"]:
                raise Fatal(f"{e['path']}: sha256 does not match manifest.json")
            # Schema 1 published the unsigned build output, so apk_sha256 is the unsigned hash.
            e.setdefault("unsigned_sha256", e["apk_sha256"])
            sign_apk(apk_bin, f, f, e["unsigned_sha256"], signing_key, pubkey)
            e.update(apk_sha256=sha256_file(f), apk_size=f.stat().st_size)
            log(f"re-signed {e['path']}")

    # 6. Remove indexes of emptied dirs, empty dirs; re-index changed dirs.
    live_dirs = {str(Path(e["path"]).parent) for e in entries}
    for d in changed_dirs - live_dirs:
        (pages / d / "packages.adb").unlink(missing_ok=True)
    remove_empty_dirs(pages)
    for d in sorted(live_dirs if resign else changed_dirs & live_dirs):
        index_dir(apk_bin, pages / d, signing_key, pubkey)

    # 7. Root files.
    manifest = {"schema": MANIFEST_SCHEMA, "pubkey_sha256": pubkey_sha256,
                "entries": [{k: e[k] for k in ENTRY_FIELDS if k in e} for e in entries]}
    manifest_file.write_text(json.dumps(manifest, indent=2) + "\n")
    shutil.copyfile(pubkey, pages_pem)
    (pages / "index.html").write_text(render_index(entries, pubkey_sha256))
    state = content_sha256(pages)
    (pages / "pages-state.json").write_text(json.dumps({"content_sha256": state}, indent=2) + "\n")

    # 8. Sanity.
    files = set(tree_files(pages))
    on_disk = {f for f in files if f.endswith(".apk")}
    in_manifest = {e["path"] for e in entries}
    if on_disk != in_manifest:
        raise Fatal(f"APKs on disk {sorted(on_disk ^ in_manifest)} disagree with manifest")
    if stale := [e["path"] for e in entries if sha256_file(pages / e["path"]) != e["apk_sha256"]]:
        raise Fatal(f"APKs whose sha256 disagrees with manifest: {stale}")
    allowed = set(ROOT_FILES) | in_manifest | {f"{d}/packages.adb" for d in live_dirs}
    if extra := files - allowed:
        raise Fatal(f"unexpected files in the tree: {sorted(extra)}")
    if missing := allowed - files:
        raise Fatal(f"missing files in the tree: {sorted(missing)}")
    for d in live_dirs:
        if len(list((pages / d).glob("*.apk"))) != 1:
            raise Fatal(f"{d} must contain exactly one APK")
    total = sum((pages / f).stat().st_size for f in files)
    if total >= MAX_TREE_BYTES:
        raise Fatal(f"tree is {total} bytes >= {MAX_TREE_BYTES}")
    changed = state != old_state
    log(f"publish: {len(entries)} APK(s), {total} bytes, content_sha256 {state}, changed={changed}")
    return changed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--matrix", required=True)
    ap.add_argument("--pages", required=True, help="gh-pages worktree (modified in place)")
    ap.add_argument("--items", help="plan items JSON (list) of this run")
    ap.add_argument("--artifacts", help="directory with one subdirectory per build item")
    ap.add_argument("--signing-key", required=True, help="EC P-256 private key PEM")
    ap.add_argument("--pubkey", default=str(APK_PUBKEY))
    ap.add_argument("--apk", default="apk", help="apk-tools v3 binary")
    args = ap.parse_args()
    try:
        changed = publish(args)
    except Fatal as e:
        log(f"error: {e}")
        return 1
    print(f"changed={'true' if changed else 'false'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
