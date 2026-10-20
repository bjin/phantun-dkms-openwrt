# AGENTS.md

Design and details are in `DEVELOPMENT.md`; `README.md` is for router users only.

## Invariants (change only with the maintainer's approval)

- `matrix.json` on `master` is the only source of truth for CI. Versions change through the `bump`
  job; targets change through pull requests.
- One build = one official OpenWrt SDK tarball (version × target/subtarget) = one APK. No SDK
  containers, no `scripts/feeds`, no `NO_DEPS=1`.
- Trust chain: vendored OpenWrt GPG key → `gpgv` on `sha256sums` → SDK sha256 pinned in
  `matrix.json`. Release pins are immutable; an upstream re-spin is an error, not a re-pin.
- Scope: OpenWrt ≥ 25.12 (apk) stable releases and main SNAPSHOT only.
- A feed directory `kmods/<kernel-id>/` only ever holds a package built for that kernel id: the
  APK's `kernel=` dependency is the only ABI guard on OpenWrt.
- Never rebuild an already published (channel, version, target, kernel, phantun version,
  pkg_release). Any change to package content must change `phantun.version` or `pkg_release`.
- Only `packages.adb` is signed, with our key from secret `APK_SIGNING_KEY`, and only in the
  `publish` job. Never publish anything from the SDK's `bin/` other than the `kmod-phantun` APK.
- `gh-pages` is a single orphan commit, force-pushed. Deployment is a separate idempotent job that
  compares the live `pages-state.json` with `gh-pages`.
- Generated feed files are deterministic; indexes are re-signed only in directories whose APK set
  changed.

## Conventions

- Python 3 standard library only (shared code in `scripts/common.py`); bash for `build-one.sh`;
  GitHub Actions pinned by commit SHA; branches `master` and `gh-pages` only.
- Write `matrix.json` through `matrix.py` (canonical order via `common.dump_matrix`).
- The recipe is rendered with `sed`; never pass `PKG_*` variables on the make command line.
- Load the module with `insmod`; OpenWrt's `modprobe` does not pass parameters.
- Scratch files go in `work/` (git-ignored).
- Before pushing: `actionlint .github/workflows/ci.yml`, `shellcheck scripts/build-one.sh`,
  `python3 -m py_compile scripts/*.py`.
- After recipe or build-script changes, rebuild a published item locally and compare APK sha256
  with the published one; builds are reproducible.
