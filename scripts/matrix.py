#!/usr/bin/env python3
"""matrix.json management (DEVELOPMENT.md, "matrix.py"): validate | fill | bump | plan."""

from __future__ import annotations

import argparse
import copy
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (  # noqa: E402
    KERNEL_RE, MAX_ITEMS, MIN_SERIES, PHANTUN_VERSION_RE, PIN_FIELDS, POINT_RE, REPO_ROOT, SCHEMA,
    SERIES_RE, SHA256_RE, TARGET_RE, Fatal, NotFound, Upstream, dump_matrix, entry_key,
    fetch_release_targets, fetch_versions, http_get, load_json, log, notice, pin_equal,
    retention, series_of, version_key, write_github_output,
)

DEFAULT_MATRIX = REPO_ROOT / "matrix.json"
TOP_KEYS = {"schema", "phantun", "policy", "targets", "snapshot_exclude", "releases", "snapshot"}
PHANTUN_KEYS = {"version", "tarball_sha256", "pkg_release", "hold"}
POLICY_KEYS = {"hold_openwrt", "build_point_releases", "retain_point_releases", "snapshot"}
PHANTUN_REPO = "bjin/phantun-dkms"
RECIPE_FILES = ("openwrt/phantun/Makefile.in", "scripts/build-one.sh")
SMOKE_TARGET = "x86/64"
PR_SNAPSHOT_TARGETS = ("mediatek/filogic", "x86/64")


def is_int(v) -> bool:
    return type(v) is int


# --------------------------------------------------------------------------- validation


def structure_errors(m) -> list[str]:
    errs: list[str] = []
    if not isinstance(m, dict):
        return ["matrix is not an object"]
    if m.get("schema") != SCHEMA:
        return [f"unknown schema {m.get('schema')!r} (expected {SCHEMA})"]
    if missing := TOP_KEYS - m.keys():
        errs.append(f"missing top-level keys: {sorted(missing)}")
    if unknown := m.keys() - TOP_KEYS:
        errs.append(f"unknown top-level keys: {sorted(unknown)}")
    if errs:
        return errs

    ph = m["phantun"]
    if not isinstance(ph, dict) or ph.keys() != PHANTUN_KEYS:
        errs.append(f"phantun must have exactly the keys {sorted(PHANTUN_KEYS)}")
    else:
        if not isinstance(ph["version"], str) or not PHANTUN_VERSION_RE.match(ph["version"]):
            errs.append(f"phantun.version {ph['version']!r} is not numeric dotted")
        if not isinstance(ph["tarball_sha256"], str) or not SHA256_RE.match(ph["tarball_sha256"]):
            errs.append("phantun.tarball_sha256 is not 64 lowercase hex")
        if not is_int(ph["pkg_release"]) or ph["pkg_release"] < 1:
            errs.append("phantun.pkg_release must be an integer >= 1")
        if not isinstance(ph["hold"], bool):
            errs.append("phantun.hold must be a boolean")

    pol = m["policy"]
    if not isinstance(pol, dict) or pol.keys() != POLICY_KEYS:
        errs.append(f"policy must have exactly the keys {sorted(POLICY_KEYS)}")
    else:
        for k in ("hold_openwrt", "snapshot"):
            if not isinstance(pol[k], bool):
                errs.append(f"policy.{k} must be a boolean")
        b, r = pol["build_point_releases"], pol["retain_point_releases"]
        if not is_int(b) or not 1 <= b <= 5:
            errs.append("policy.build_point_releases must be an integer 1..5")
        elif not is_int(r) or not b <= r <= 5:
            errs.append("policy.retain_point_releases must be an integer build_point_releases..5")

    targets = m["targets"]
    if not isinstance(targets, list) or not all(isinstance(t, str) for t in targets):
        errs.append("targets must be a list of strings")
        targets = []
    else:
        for t in targets:
            if not TARGET_RE.match(t):
                errs.append(f"target {t!r} does not match {TARGET_RE.pattern}")
        if targets != sorted(set(targets)):
            errs.append("targets must be sorted and unique")
    excl = m["snapshot_exclude"]
    if not isinstance(excl, list) or not all(isinstance(t, str) for t in excl):
        errs.append("snapshot_exclude must be a list of strings")
    else:
        if excl != sorted(set(excl)):
            errs.append("snapshot_exclude must be sorted and unique")
        if extra := set(excl) - set(targets):
            errs.append(f"snapshot_exclude entries not in targets: {sorted(extra)}")

    rel = m["releases"]
    if not isinstance(rel, dict):
        errs.append("releases must be an object")
    else:
        for s, points in rel.items():
            if not SERIES_RE.match(s) or version_key(s) < MIN_SERIES:
                errs.append(f"series {s!r} must match {SERIES_RE.pattern} and be >= 25.12")
                continue
            if not isinstance(points, dict):
                errs.append(f"releases.{s} must be an object")
                continue
            for p, pins in points.items():
                if not POINT_RE.match(p) or series_of(p) != s:
                    errs.append(f"point release {p!r} must match {POINT_RE.pattern} and belong to {s}")
                    continue
                if not isinstance(pins, dict):
                    errs.append(f"releases.{s}.{p} must be an object")
                    continue
                for t, pin in pins.items():
                    errs.extend(pin_errors(f"releases.{s}.{p}", t, pin))
    snap = m["snapshot"]
    if not isinstance(snap, dict):
        errs.append("snapshot must be an object")
    else:
        for t, pin in snap.items():
            errs.extend(pin_errors("snapshot", t, pin))
    return errs


def pin_errors(where: str, target, pin) -> list[str]:
    errs = []
    if not TARGET_RE.match(target):
        errs.append(f"{where}: pin key {target!r} is not a target")
    if pin is None:
        return errs
    if not isinstance(pin, dict):
        return errs + [f"{where}.{target}: pin must be null or an object"]
    for k in PIN_FIELDS:
        v = pin.get(k)
        if not isinstance(v, str) or not v:
            errs.append(f"{where}.{target}.{k} missing or empty")
    if isinstance(pin.get("kernel"), str) and not KERNEL_RE.match(pin["kernel"]):
        errs.append(f"{where}.{target}.kernel {pin['kernel']!r} does not match {KERNEL_RE.pattern}")
    if isinstance(pin.get("sdk_sha256"), str) and not SHA256_RE.match(pin["sdk_sha256"]):
        errs.append(f"{where}.{target}.sdk_sha256 is not 64 lowercase hex")
    return errs


def item_count(m: dict) -> int:
    points = sum(len(p) for p in m["releases"].values())
    return len(m["targets"]) * points + len(m["snapshot"])


def shape_errors(m: dict) -> list[str]:
    """The offline part of --complete (requires structure_errors(m) == [])."""
    errs = []
    targets = set(m["targets"])
    n = m["policy"]["build_point_releases"]
    for s, points in m["releases"].items():
        if len(points) > n:
            errs.append(f"series {s} lists {len(points)} point releases > build_point_releases {n}")
        for p, pins in points.items():
            if missing := targets - pins.keys():
                errs.append(f"{p}: no pin for {sorted(missing)}")
            if extra := pins.keys() - targets:
                errs.append(f"{p}: pins for unlisted targets {sorted(extra)}")
    snap = m["snapshot"]
    if m["policy"]["snapshot"]:
        want = targets - set(m["snapshot_exclude"])
        if missing := want - snap.keys():
            errs.append(f"snapshot: no pin for {sorted(missing)}")
        if extra := snap.keys() - want:
            errs.append(f"snapshot: pins for unlisted/excluded targets {sorted(extra)}")
    elif snap:
        errs.append("snapshot must be {} when policy.snapshot is false")
    if (c := item_count(m)) > MAX_ITEMS:
        errs.append(f"build matrix size {c} > {MAX_ITEMS}")
    return errs


def upstream_errors(m: dict, up: Upstream) -> list[str]:
    errs = []
    for points in m["releases"].values():
        for p, pins in points.items():
            for t, pin in pins.items():
                live = up.pin("release", p, t)
                if not pin_equal(pin, live):
                    errs.append(f"release pin {p} {t} differs from upstream (re-spin?): "
                                f"stored {pin} upstream {live}")
    return errs


def cmd_validate(args) -> int:
    m = load_json(args.matrix)
    errs = structure_errors(m)
    if not errs and args.complete:
        errs = shape_errors(m)
        if not errs:
            errs = upstream_errors(m, Upstream())
    for e in errs:
        log(f"invalid: {e}")
    if errs:
        return 1
    log(f"{args.matrix}: {'complete' if args.complete else 'structure'} ok")
    return 0


# --------------------------------------------------------------------------- fill / bump


class Changes(list):
    def add(self, msg: str) -> None:
        log(f"change: {msg}")
        self.append(msg)


def apply_local_policy(m: dict, ch: Changes) -> None:
    targets = set(m["targets"])
    n = m["policy"]["build_point_releases"]
    for s, points in m["releases"].items():
        for p in sorted(points, key=version_key, reverse=True)[n:]:
            del points[p]
            ch.add(f"{s}: drop {p} (build_point_releases={n})")
        for p, pins in points.items():
            for t in sorted(pins.keys() - targets):
                del pins[t]
                ch.add(f"{p}: drop pin of unlisted target {t}")
    if not m["policy"]["snapshot"]:
        if m["snapshot"]:
            ch.add("snapshot: clear (policy.snapshot is false)")
        m["snapshot"] = {}
    else:
        want = targets - set(m["snapshot_exclude"])
        for t in sorted(m["snapshot"].keys() - want):
            del m["snapshot"][t]
            ch.add(f"SNAPSHOT: drop pin of unlisted/excluded target {t}")


def fill_missing_release_pins(m: dict, up: Upstream, ch: Changes) -> None:
    for points in m["releases"].values():
        for p, pins in points.items():
            for t in m["targets"]:
                if t not in pins:
                    pins[t] = up.pin("release", p, t)
                    ch.add(f"{p} {t}: pin {describe(pins[t])}")


def fill_missing_snapshot_pins(m: dict, up: Upstream, ch: Changes) -> None:
    if not m["policy"]["snapshot"]:
        return
    for t in m["targets"]:
        if t in m["snapshot_exclude"] or t in m["snapshot"]:
            continue
        m["snapshot"][t] = up.pin("snapshot", "SNAPSHOT", t)
        ch.add(f"SNAPSHOT {t}: pin {describe(m['snapshot'][t])}")


def describe(pin: dict | None) -> str:
    return "null (absent upstream)" if pin is None else f"{pin['kernel']} {pin['version_code']}"


def fill(m: dict, up: Upstream) -> Changes:
    ch = Changes()
    apply_local_policy(m, ch)
    fill_missing_release_pins(m, up, ch)
    fill_missing_snapshot_pins(m, up, ch)
    return ch


def github_headers() -> dict:
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if tok := os.environ.get("GITHUB_TOKEN"):
        h["Authorization"] = f"Bearer {tok}"
    return h


def check_tarball_layout(data: bytes, name: str) -> None:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        names = {n.removeprefix("./") for n in tf.getnames()}
    for required in ("configure", "Kbuild"):
        if required not in names:
            raise Fatal(f"{name}: no top-level {required!r}; tarball layout changed, "
                        "the recipe's Build/Prepare needs review")


def bump_phantun(m: dict, ch: Changes) -> None:
    ph = m["phantun"]
    if ph["hold"]:
        log("phantun: hold, not checking for releases")
        return
    rel = json.loads(http_get(f"https://api.github.com/repos/{PHANTUN_REPO}/releases/latest",
                              headers=github_headers()))
    tag = rel["tag_name"]
    version = tag.removeprefix("v")
    if not PHANTUN_VERSION_RE.match(version):
        raise Fatal(f"latest phantun-dkms release tag {tag!r} is not a numeric version")
    asset_name = f"phantun-dkms_{version}.tar.gz"
    assets = [a for a in rel.get("assets", []) if a["name"] == asset_name]
    if len(assets) != 1:
        raise Fatal(f"release {tag} has no asset {asset_name}")
    url = f"https://github.com/{PHANTUN_REPO}/releases/download/{tag}/{asset_name}"
    data = http_get(url, timeout=300)
    sha = hashlib.sha256(data).hexdigest()
    if version == ph["version"]:
        if sha != ph["tarball_sha256"]:
            raise Fatal(f"{asset_name} sha256 changed: pinned {ph['tarball_sha256']}, now {sha}")
        log(f"phantun: {version} is current")
        return
    check_tarball_layout(data, asset_name)
    ch.add(f"phantun-dkms {ph['version']} -> {version} (sha256 {sha}), pkg_release 1")
    ph.update(version=version, tarball_sha256=sha, pkg_release=1)


def tracked_series(versions: dict) -> set[str]:
    out = set()
    for k in ("stable_version", "oldstable_version"):
        v = versions.get(k) or ""
        parts = v.split(".")
        if len(parts) >= 2 and all(x.isdigit() for x in parts[:2]):
            s = ".".join(parts[:2])
            if version_key(s) >= MIN_SERIES:
                out.add(s)
    return out


def version_complete(m: dict, up: Upstream, s: str, v: str, current: dict) -> dict | None:
    """Pins for every target of point release v, or None while v is incomplete upstream."""
    pins = {}
    try:
        for t in m["targets"]:
            pins[t] = up.pin("release", v, t)
    except (NotFound, Fatal) as e:
        if isinstance(e, Fatal) and "gpgv" in str(e):
            raise
        notice(f"{v}: not complete upstream yet ({e}); keeping series {s} unchanged")
        return None
    if current:
        ref = min(current, key=lambda p: (version_key(p) > version_key(v),
                                          abs(version_key(p)[2] - version_key(v)[2])))
        required = {t for t, pin in current[ref].items() if pin is not None and t in pins}
        why = f"non-null in {ref}"
    else:
        listed = fetch_release_targets(v)
        if listed is None:
            notice(f"{v}: .targets.json not published yet; keeping series {s} unchanged")
            return None
        required = set(m["targets"]) & listed
        why = f"listed in {v}/.targets.json"
    if missing := sorted(t for t in required if pins[t] is None):
        notice(f"{v}: targets {missing} ({why}) not uploaded yet; keeping series {s} unchanged")
        return None
    return pins


def bump_releases(m: dict, up: Upstream, ch: Changes) -> None:
    if m["policy"]["hold_openwrt"]:
        log("releases: hold_openwrt, keeping listed point releases")
        return
    versions = fetch_versions()
    tracked = tracked_series(versions)
    log(f"tracked series: {sorted(tracked)}")
    n = m["policy"]["build_point_releases"]
    for s in sorted(m["releases"].keys() - tracked):
        del m["releases"][s]
        ch.add(f"drop series {s} (no longer stable/oldstable)")
    for s in sorted(tracked, key=version_key):
        cands = sorted({v for v in versions.get("versions_list", [])
                        if POINT_RE.match(v) and series_of(v) == s}, key=version_key, reverse=True)
        window = cands[:n]
        current = m["releases"].get(s, {})
        if not window or set(window) == current.keys():
            continue
        new: dict[str, dict] = {}
        for v in window:
            if v in current:
                continue
            pins = version_complete(m, up, s, v, current)
            if pins is None:
                break
            new[v] = pins
        else:
            m["releases"][s] = {v: current[v] if v in current else new[v] for v in window}
            for v in sorted(current.keys() - set(window), key=version_key):
                ch.add(f"{s}: {v} left the build window")
            for v in sorted(new, key=version_key):
                ch.add(f"{s}: add {v} (" + ", ".join(f"{t} {describe(p)}" for t, p in new[v].items()) + ")")


def bump_snapshot(m: dict, manifest: dict, up: Upstream, ch: Changes) -> None:
    if not m["policy"]["snapshot"]:
        return
    if m["policy"]["hold_openwrt"]:
        fill_missing_snapshot_pins(m, up, ch)
        return
    ph = m["phantun"]
    published = {entry_key(e) for e in manifest.get("entries", [])}
    for t in m["targets"]:
        if t in m["snapshot_exclude"]:
            continue
        live = up.pin("snapshot", "SNAPSHOT", t)
        if t not in m["snapshot"]:
            m["snapshot"][t] = live
            ch.add(f"SNAPSHOT {t}: pin {describe(live)}")
            continue
        stored = m["snapshot"][t]
        if stored is None or live is None:
            if not pin_equal(stored, live):
                m["snapshot"][t] = live
                ch.add(f"SNAPSHOT {t}: {describe(stored)} -> {describe(live)}")
            continue
        if live["kernel"] != stored["kernel"]:
            m["snapshot"][t] = live
            ch.add(f"SNAPSHOT {t}: new kernel {describe(stored)} -> {describe(live)}")
            continue
        key = ("snapshot", "SNAPSHOT", t, stored["kernel"], ph["version"], int(ph["pkg_release"]))
        if key not in published and live["sdk_sha256"] != stored["sdk_sha256"]:
            m["snapshot"][t] = live
            ch.add(f"SNAPSHOT {t}: refresh stale unbuilt pin {describe(stored)} -> {describe(live)}")


def bump(m: dict, manifest: dict, up: Upstream) -> Changes:
    ch = Changes()
    apply_local_policy(m, ch)
    bump_phantun(m, ch)
    bump_releases(m, up, ch)
    fill_missing_release_pins(m, up, ch)
    bump_snapshot(m, manifest, up, ch)
    return ch


def cmd_fill_or_bump(args) -> int:
    m = load_json(args.matrix)
    if errs := structure_errors(m):
        for e in errs:
            log(f"invalid: {e}")
        return 1
    m = copy.deepcopy(m)
    up = Upstream()
    if args.cmd == "fill":
        ch = fill(m, up)
    else:
        ch = bump(m, load_json(args.manifest), up)
    if errs := structure_errors(m) or shape_errors(m):
        for e in errs:
            log(f"result invalid: {e}")
        return 1
    out = args.out if getattr(args, "in_memory", False) else args.matrix
    Path(out).write_text(dump_matrix(m))
    for c in ch:
        print(c)
    log(f"{args.cmd}: {len(ch)} change(s), wrote {out}")
    return 0


# --------------------------------------------------------------------------- plan


def make_item(m: dict, channel: str, series: str | None, version: str, target: str,
              pin: dict) -> dict:
    ph = m["phantun"]
    slug = target.replace("/", "-")
    item = {
        "id": f"release-{version}-{slug}" if channel == "release" else f"snapshot-{slug}",
        "channel": channel, "series": series, "version": version, "target": target,
        **{k: pin[k] for k in PIN_FIELDS},
        "phantun_version": ph["version"], "phantun_sha256": ph["tarball_sha256"],
        "pkg_release": ph["pkg_release"], "smoke": target == SMOKE_TARGET,
    }
    return item


def desired_items(m: dict) -> list[dict]:
    items = []
    for s, points in m["releases"].items():
        for p, pins in points.items():
            for t in m["targets"]:
                if pins.get(t) is not None:
                    items.append(make_item(m, "release", s, p, t, pins[t]))
    if m["policy"]["snapshot"]:
        for t, pin in m["snapshot"].items():
            if pin is not None and t not in m["snapshot_exclude"]:
                items.append(make_item(m, "snapshot", None, "SNAPSHOT", t, pin))
    return items


def item_key(it: dict) -> tuple:
    return (it["channel"], it["version"], it["target"], it["kernel"], it["phantun_version"],
            int(it["pkg_release"]))


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=REPO_ROOT, check=True, capture_output=True,
                          text=True).stdout


def newest_points(m: dict) -> list[tuple[str, str]]:
    return [(s, max(points, key=version_key)) for s, points in m["releases"].items() if points]


def pr_items(m: dict, base_ref: str, pr_title: str) -> list[dict]:
    try:
        base = json.loads(git("show", f"{base_ref}:matrix.json"))
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        base = None
    changed_files = set(git("diff", "--name-only", f"{base_ref}...HEAD").split())
    head_raw = load_json(DEFAULT_MATRIX)  # the PR's own matrix.json, before in-memory fill

    recipe_changed = bool(changed_files & set(RECIPE_FILES))
    if recipe_changed and base is not None:
        same = (base.get("phantun", {}).get("version") == head_raw["phantun"]["version"]
                and base.get("phantun", {}).get("pkg_release") == head_raw["phantun"]["pkg_release"])
        if same and "[no-rebuild]" not in pr_title:
            raise Fatal(f"{' or '.join(RECIPE_FILES)} changed but neither phantun.version nor "
                        "pkg_release did; bump pkg_release or put [no-rebuild] in the PR title")

    items: dict[str, dict] = {}

    def add(channel, s, v, t, pin):
        if pin is not None:
            it = make_item(m, channel, s, v, t, pin)
            items[it["id"]] = it

    snapshot_on = m["policy"]["snapshot"]
    excl = set(m["snapshot_exclude"])
    if base is None or structure_errors(base):
        changed_targets = set(m["targets"])
    else:
        changed_targets = set(m["targets"]) - set(base["targets"])
        for t in m["targets"]:
            for s, points in m["releases"].items():
                for p, pins in points.items():
                    bpins = base["releases"].get(s, {}).get(p, {})
                    if t in bpins and not pin_equal(bpins[t], pins.get(t)):
                        changed_targets.add(t)
            if t in base["snapshot"] and not pin_equal(base["snapshot"][t], m["snapshot"].get(t)):
                changed_targets.add(t)
            if t in set(base["snapshot_exclude"]) - excl:
                changed_targets.add(t)
    for t in sorted(changed_targets):
        for s, p in newest_points(m):
            add("release", s, p, t, m["releases"][s][p].get(t))
        if snapshot_on and t not in excl:
            add("snapshot", None, "SNAPSHOT", t, m["snapshot"].get(t))
    if changed_files & {"scripts/build-one.sh"} or any(f.startswith("openwrt/") for f in changed_files):
        for t in m["targets"]:
            for s, p in newest_points(m):
                add("release", s, p, t, m["releases"][s][p].get(t))
        if snapshot_on:
            for t in PR_SNAPSHOT_TARGETS:
                if t in m["targets"] and t not in excl:
                    add("snapshot", None, "SNAPSHOT", t, m["snapshot"].get(t))
    return sorted(items.values(), key=lambda it: it["id"])


def cmd_plan(args) -> int:
    m = load_json(args.matrix)
    if errs := structure_errors(m) or shape_errors(m):
        for e in errs:
            log(f"invalid: {e}")
        return 1
    manifest = load_json(args.manifest)
    entries = manifest.get("entries", [])
    if args.pr_base:
        items = pr_items(m, args.pr_base, args.pr_title or os.environ.get("PR_TITLE", ""))
        publish = False
    else:
        published = {tuple(entry_key(e)) for e in entries}
        items = [it for it in desired_items(m) if item_key(it) not in published]
        _, deleted = retention(entries, m)
        for e, why in deleted:
            log(f"would prune {e['path']}: {why}")
        publish = bool(items) or bool(deleted)
    if len(items) > MAX_ITEMS:
        raise Fatal(f"{len(items)} build items > {MAX_ITEMS}")
    for it in items:
        log(f"build: {it['id']} {it['kernel']} phantun {it['phantun_version']}-r{it['pkg_release']}")
    result = {"count": len(items), "publish": publish, "items": items}
    print(json.dumps(result, indent=2))
    if args.github_output:
        write_github_output({"items": json.dumps(items, separators=(",", ":")),
                             "count": str(len(items)), "publish": "true" if publish else "false"})
    log(f"plan: {len(items)} item(s), publish={publish}")
    return 0


# --------------------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("validate")
    g = v.add_mutually_exclusive_group(required=True)
    g.add_argument("--structure", action="store_true")
    g.add_argument("--complete", action="store_true")
    v.add_argument("--matrix", default=str(DEFAULT_MATRIX))

    f = sub.add_parser("fill")
    f.add_argument("--matrix", default=str(DEFAULT_MATRIX))
    f.add_argument("--manifest", help="accepted for symmetry with bump; unused")
    f.add_argument("--in-memory", action="store_true", help="do not rewrite --matrix")
    f.add_argument("--out", help="with --in-memory: where to write the filled matrix")

    b = sub.add_parser("bump")
    b.add_argument("--matrix", default=str(DEFAULT_MATRIX))
    b.add_argument("--manifest", required=True)

    p = sub.add_parser("plan")
    p.add_argument("--matrix", default=str(DEFAULT_MATRIX))
    p.add_argument("--manifest", required=True)
    p.add_argument("--pr-base", help="git ref of the PR base (PR mode)")
    p.add_argument("--pr-title", help="PR title (default: $PR_TITLE)")
    p.add_argument("--github-output", action="store_true", help="also write items/count/publish to $GITHUB_OUTPUT")

    args = ap.parse_args()
    if args.cmd == "fill" and args.in_memory and not args.out:
        ap.error("fill --in-memory requires --out")
    try:
        if args.cmd == "validate":
            return cmd_validate(args)
        if args.cmd in ("fill", "bump"):
            return cmd_fill_or_bump(args)
        return cmd_plan(args)
    except Fatal as e:
        log(f"error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
