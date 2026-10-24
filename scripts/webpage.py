#!/usr/bin/env python3
"""Render the feed page (DEVELOPMENT.md, "Feed page"): web/index.html with its @NAME@ placeholders
filled from the manifest entries and matrix.json's targets, and web/style.css and web/script.js
inlined unminified, so the published index.html is one self-contained file. Deterministic: same
inputs, same bytes.

publish.py writes the page with render_index(); matrix.py plan compares it with the published one.
Standalone, it renders a local preview:

  curl -fsSo work/manifest.json https://bjin.github.io/phantun-dkms-openwrt/manifest.json
  python3 scripts/webpage.py --manifest work/manifest.json --out work/index.html
"""

from __future__ import annotations

import argparse
import html
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import APK_PUBKEY, REPO_ROOT, Fatal, load_json, log, sha256_file  # noqa: E402

WEB_DIR = REPO_ROOT / "web"
PLACEHOLDER_RE = re.compile(r"@([A-Z][A-Z0-9_]*)@")
TABLE_HEAD = ("<thead><tr><th>Target / arch</th><th>Kernel</th><th>Revision</th>"
              "<th>Package</th><th>Index</th></tr></thead>")


def row(e: dict, more: bool) -> str:
    esc = html.escape
    kernel_version, _, vermagic = e["kernel"].rpartition("-")
    kdir, apk = e["path"].rsplit("/", 1)
    apk_version = apk.removeprefix("kmod-phantun-").removesuffix(".apk")
    return (f'<tr data-target="{esc(e["target"])}"' + (' class="more">' if more else ">")
            + f'<td class="target">{esc(e["target"])}<span class="arch">{esc(e["arch"])}</span></td>'
            f'<td data-label="Kernel"><div class="kernel">{esc(kernel_version)}'
            f'<span class="vermagic">-{esc(vermagic)}</span></div></td>'
            f'<td data-label="Revision"><span class="mono">{esc(e["version_code"])}</span></td>'
            f'<td class="action"><a class="pkg" href="{esc(e["path"])}" title="{esc(apk)}">'
            f'<svg class="icon"><use href="#i-download"/></svg>{esc(apk_version)}</a></td>'
            f'<td class="action"><a class="idx" href="{esc(kdir)}/packages.adb">packages.adb</a></td>'
            "</tr>")


def table(entries: list[dict]) -> str:
    """Entries in display order; further rows of the same target are marked to group them."""
    rows = "\n".join(row(e, i > 0 and e["target"] == entries[i - 1]["target"])
                     for i, e in enumerate(entries))
    return f'<div class="table-card"><table>\n{TABLE_HEAD}\n<tbody>\n{rows}\n</tbody>\n</table></div>'


def pkg_group(heading: str, entries: list[dict]) -> str:
    """A unit the target filter hides when none of its rows match."""
    return f'<div class="pkg-group">\n{heading}{table(entries)}\n</div>'


def group(entries: list[dict], key: str) -> list[list[dict]]:
    """Entries grouped by entries[key], in order of appearance."""
    groups: dict[str, list[dict]] = {}
    for e in entries:
        groups.setdefault(e[key], []).append(e)
    return list(groups.values())


def plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def target_tile(target: str, arch: str | None) -> str:
    esc = html.escape
    return ('      <button type="button" class="target-tile" '
            f'data-target="{esc(target)}" aria-pressed="true">'
            '<svg class="icon"><use href="#i-check"/></svg>'
            f'<span class="target-name">{esc(target).replace("/", "/<wbr>", 1)}</span>'
            f'<span class="target-arch">{esc(arch) if arch else "no packages yet"}</span></button>')


def render_index(entries: list[dict], pem_sha256: str, targets: list[str]) -> str:
    """entries in sort_entries order (version, target, version_code ascending), as in manifest.json;
    targets is matrix.json's list."""
    esc = html.escape
    rel = [e for e in entries if e["channel"] == "release"]
    snap = [e for e in entries if e["channel"] == "snapshot"]
    none = '<p class="empty">None.</p>'
    releases = "\n".join(  # newest point release first
        pkg_group(f'<h3>OpenWrt {esc(es[0]["version"])} '
                  f'<span class="count">{plural(len(es), "package")}</span></h3>\n', es)
        for es in reversed(group(rel, "version"))) or none
    # Targets in order, each target's newest kernel first.
    snapshot = pkg_group("", [e for es in group(snap, "target") for e in reversed(es)]) if snap else none
    arch = {e["target"]: e["arch"] for e in entries}
    phantun = sorted({(e["phantun_version"], e["pkg_release"]) for e in entries})
    chips = "\n".join(
        f'      <li class="chip primary">phantun-dkms {esc(v)}, package release r{esc(str(r))}</li>'
        for v, r in phantun) or '      <li class="chip">phantun-dkms: none</li>'
    values = {
        "STYLE": (WEB_DIR / "style.css").read_text(encoding="utf-8"),
        "SCRIPT": (WEB_DIR / "script.js").read_text(encoding="utf-8"),
        "PHANTUN_CHIPS": chips,
        "RELEASE_COUNT": str(len(rel)),
        "SNAPSHOT_COUNT": str(len(snap)),
        "PEM_SHA256": esc(pem_sha256),
        "TARGET_COUNT": str(len(targets)),
        "TARGETS": "\n".join(target_tile(t, arch.get(t)) for t in targets),
        "RELEASES": releases,
        "SNAPSHOT": snapshot,
    }
    used = set()

    def fill(m: re.Match) -> str:
        name = m.group(1)
        if name not in values:
            raise Fatal(f"web/index.html: unknown placeholder @{name}@")
        used.add(name)
        return values[name]

    page = PLACEHOLDER_RE.sub(fill, (WEB_DIR / "index.html").read_text(encoding="utf-8"))
    if unused := sorted(set(values) - used):
        raise Fatal(f"web/index.html: missing placeholders {unused}")
    return page


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--manifest", required=True, help="manifest.json whose entries the page lists")
    ap.add_argument("--matrix", default=str(REPO_ROOT / "matrix.json"), help="matrix.json whose targets the page lists")
    ap.add_argument("--pubkey", default=str(APK_PUBKEY), help="public key whose sha256 the page shows")
    ap.add_argument("--out", required=True, help="where to write index.html")
    args = ap.parse_args()
    try:
        page = render_index(load_json(args.manifest).get("entries", []), sha256_file(Path(args.pubkey)),
                            load_json(args.matrix)["targets"])
    except Fatal as e:
        log(f"error: {e}")
        return 1
    Path(args.out).write_text(page)
    log(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
