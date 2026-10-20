# Development

Design and operation of the `kmod-phantun` feed builder. User-facing documentation is in
`README.md`; agent working notes are in `AGENTS.md`.

## Repository layout

```
README.md, DEVELOPMENT.md, AGENTS.md
matrix.json                        desired build state; HEAD of master defines CI
keys/
  openwrt-8A8BC12F46B836C0F9CDB36F1D53D1877742E911.asc   OpenWrt build key (verifies sha256sums)
  phantun-openwrt.pem              our apk public key
openwrt/phantun/Makefile.in        OpenWrt package recipe template
scripts/
  common.py                        shared helpers (HTTP, gpgv, upstream pins, retention, matrix I/O)
  matrix.py                        validate | fill | bump | plan
  build-one.sh                     build one item with the official SDK (CI and humans)
  smoke-x86.py                     QEMU install/load test for x86/64 items
  publish.py                       assemble the gh-pages tree
.github/workflows/ci.yml           the only workflow
```

Branches: `master` (code + `matrix.json`) and `gh-pages` (the published feed, one orphan commit,
force-pushed). Python scripts use the standard library only; `ubuntu-24.04` runners ship 3.12.

## How OpenWrt kmods work

Facts the design depends on (verified on 25.12.6 and SNAPSHOT 2026-10):

- **Every target/subtarget has its own kernel build**, identified as
  `<linux_version>-<linux_release>-<vermagic>`, e.g. `6.12.112-1-f0a7348f9cb959e1bf4d0447669b031e`
  (`profiles.json` → `linux_kernel.{version,release,vermagic}`; the same string names the official
  `kmods/<id>/` directory). `vermagic` is an MD5 over the kernel config; no two subtargets share
  it, so a `.ko` is never reusable across subtargets.
- **Every kmod APK depends on the exact kernel package**,
  `kernel=6.12.112~f0a7348f9cb959e1bf4d0447669b031e-r1`. This is the only ABI guard: kernels have
  `CONFIG_MODULE_STRIPPED=y`, no `CONFIG_MODVERSIONS`, and OpenWrt's module-strip patch skips the
  vermagic check. A feed directory must therefore only ever contain packages built for that
  directory's kernel id. Directory names use `-`; the `~` form exists only in the dependency.
- **The SDK is sufficient**: prepared kernel tree, `Module.symvers`, cross toolchain, the kernel
  hash in `include/kernel.mk` (`LINUX_VERMAGIC:=$(if $(LINUX_VERMAGIC),$(LINUX_VERMAGIC),<hash>)`),
  and `package/kernel/linux`, so `+kmod-nf-conntrack` resolves without feeds.
- **phantun.ko's only kmod dependency is `kmod-nf-conntrack`** (`nf_conntrack`, `nf_defrag_ipv4`,
  `nf_defrag_ipv6` all ship in it); it is preinstalled on default images (firewall4).
- **All OpenWrt kernels have `CONFIG_WERROR=y`**: every phantun-dkms `configure` probe must compile
  warning-free or it silently reports "no".
- **apk trust**: OpenWrt signs only `packages.adb` (`apk mkndx --sign`); a package installed from
  a repository is trusted through the index, which records its identity (hash of the APK's ADB
  metadata block) and `file-size`. A standalone `apk add ./x.apk` needs a signature inside the
  APK (`apk adbsign`); it is a separate block, so signing keeps the identity but changes the file
  size. Keys are EC P-256 PEM in `/etc/apk/keys/`.
- **Kernel identity churn**: a stable point release never changes; every point release has a new
  identity. SNAPSHOT `mediatek/filogic` changed identity ~every 4 days; the SNAPSHOT SDK file name
  has no version and is overwritten in place, so a SNAPSHOT identity can only be built while its
  SDK is live.
- **Upstream signatures**: `sha256sums.asc` is signed by subkey
  `92C561DE55AE6552F3C736B82B0151090606D1D9` of "OpenWrt Build System (Nitrokey3)", primary
  `8A8B C12F 46B8 36C0 F9CD B36F 1D53 D187 7742 E911`. The vendored copy comes from
  `https://raw.githubusercontent.com/openwrt/docker/main/keys/0x1D53D1877742E911.asc` (subkey expiry
  extended to 2033; openwrt.org's signatures page still shows 2025, ignore that). `sha256sums`
  also lists `profiles.json` and `kmods/<id>/packages.adb`, so both are verified through it.
- Old releases move from downloads.openwrt.org to archive.openwrt.org; downloads fall back there.
- `https://downloads.openwrt.org/releases/<ver>/.targets.json` lists the targets of a release.

## `matrix.json`

```jsonc
{
  "schema": 1,
  "phantun": {
    "version": "0.6.0.1",          // phantun-dkms GitHub release tag without "v"; numeric dotted only
    "tarball_sha256": "…",         // sha256 of release asset phantun-dkms_<version>.tar.gz
    "pkg_release": 1,              // OpenWrt PKG_RELEASE (recipe revision)
    "hold": true                   // true: bump never changes "phantun"
  },
  "policy": {
    "hold_openwrt": true,          // true: bump never adds/removes point releases or series and
                                   //       never refreshes snapshot pins (fill still fills gaps)
    "build_point_releases": 1,     // build window: newest N point releases per series (1..5)
    "retain_point_releases": 5,    // storage cap per series on Pages (build_point_releases..5)
    "snapshot": false              // false: no SNAPSHOT pins, builds or published packages
  },
  "targets": ["mediatek/filogic"], // sorted, unique "target/subtarget"
  "snapshot_exclude": [],          // targets whose SNAPSHOT builds are disabled
  "releases": {                    // series -> point release -> target -> pin (null = absent upstream)
    "25.12": { "25.12.6": { "mediatek/filogic": {
      "sdk": "openwrt-sdk-25.12.6-mediatek-filogic_gcc-14.3.0_musl.Linux-x86_64.tar.zst",
      "sdk_sha256": "1c04ac47…", "kernel": "6.12.112-1-f0a7348f…", "arch": "aarch64_cortex-a53",
      "version_code": "r33311-981c978eff" } } }
  },
  "snapshot": {}                   // target -> pin of the currently live SNAPSHOT SDK
}
```

Pins come from `profiles.json` and the GPG-verified `sha256sums`. `matrix.py` writes the file in a
canonical order (2-space indent, newest versions first); hand edits should keep it.

### Invariants

`validate --structure` (offline; every committed or proposed file): known schema, exact key sets
for top level/`phantun`/`policy`, types and ranges above, `targets` sorted/unique/valid,
`snapshot_exclude` ⊆ `targets`, series `YY.MM` ≥ `25.12`, point releases `YY.MM.N` inside their
series, pins `null` or complete (`kernel` matches `^\d+\.\d+\.\d+-\d+-[0-9a-f]{32}$`). Missing
pins, pins of unlisted targets and surplus point releases are allowed here: they are what a human
edit of `targets` or `policy` leaves behind.

`validate --complete` (structure plus): each series lists ≤ `build_point_releases` point releases;
every point release has a pin entry for every target and none for unlisted ones; `snapshot` has a
pin for every non-excluded target when `policy.snapshot`, else is `{}`; every release pin is
re-fetched and must equal upstream (a difference means an upstream re-spin and needs a human);
`len(targets) × point releases + len(snapshot)` ≤ 256 (GitHub matrix limit). `master` must pass
it after every `bump` job; a PR only after an in-memory `fill`.

### `pkg_release` rule

APK version = `<linux_version>.<phantun_version>-r<pkg_release>`, e.g. `6.12.112.0.6.0.1-r1`.
Any change to package content must change the version, otherwise routers keep the old file and
Pages would serve two different files under one name.

- `bump` resets `pkg_release` to 1 when `phantun.version` changes.
- A PR that changes `openwrt/phantun/Makefile.in` or `scripts/build-one.sh` must change
  `phantun.version` or `pkg_release`; PR CI enforces it unless the PR title contains
  `[no-rebuild]` (comment/whitespace-only changes).

## `scripts/matrix.py`

Upstream fetch for one (channel, version, target): base
`downloads.openwrt.org/releases/<ver>/targets/<t>/<s>/` (fallback `archive.openwrt.org`) or
`downloads.openwrt.org/snapshots/targets/<t>/<s>/`. `profiles.json` 404 on every base → pin `null`
(target absent). Otherwise `sha256sums` + `.asc` are verified with `gpgv` against the vendored key,
exactly one `openwrt-sdk-*.Linux-x86_64.tar.zst` line must exist, `profiles.json` must match its
`sha256sums` entry, `target`/`version_number` must match, and `kmods/<kernel>/packages.adb` must be
listed and reachable. SNAPSHOT inconsistencies (files from different buildbot runs) are retried
3× with 60 s pauses; anything else fails the command (no partial matrices). Exception for
releases: upstream sometimes rewrites a release's `profiles.json` without re-signing `sha256sums`
(25.12.6 `qualcommax/ipq50xx` on 2026-10-20: 5 of 14 profiles left, other fields unchanged). The
pin is then accepted only if its kernel is the single `kmods/<id>/` in `sha256sums` and its
`version_code` equals the signed `version.buildinfo`; `arch` stays unsigned but `build-one.sh`
fails unless the APK built by the verified SDK has that arch. A notice is logged.

Subcommands:

- `validate --structure|--complete [--matrix F]`.
- `fill [--in-memory --out F]`: local policy (trim each series to the newest
  `build_point_releases`, drop pins of unlisted targets, clear/trim `snapshot`), then fetch missing
  pins only. Never adds versions or series and never bumps phantun.
- `bump --manifest F`: `fill`'s local policy, then
  1. phantun (unless `hold`): `GET /repos/bjin/phantun-dkms/releases/latest` (excludes drafts and
     pre-releases = "stable"), asset must be `phantun-dkms_<v>.tar.gz` with top-level `configure`
     and `Kbuild`. New version → set version, sha256, `pkg_release=1`. Same version but different
     sha256 → fail.
  2. releases (unless `hold_openwrt`): tracked series = series ≥ 25.12 of `.versions.json`'s
     `stable_version`/`oldstable_version`; window = newest `build_point_releases` final versions
     (`YY.MM.N`, no `-rc`) per series. A new point release is added only when complete upstream:
     every target that has a non-null pin in the nearest listed point release of the same series
     must fetch a non-null pin; for a new series (no reference), every matrix target listed in the
     version's `.targets.json`. Otherwise the series is left unchanged with a notice (OpenWrt
     uploads targets over hours). Versions leaving the window and untracked series are dropped
     from the matrix (their packages stay until retention prunes them).
  3. missing release pins are filled.
  4. SNAPSHOT (only if `policy.snapshot`; with `hold_openwrt` only missing pins): for each
     non-excluded target, replace the stored pin when the kernel id changed, or when the stored
     (kernel, phantun version, pkg_release) is not in the manifest and the SDK sha256 changed
     (stale pin for a build that is still needed). Otherwise keep it, so a rebuilt SDK with the
     same kernel does not cause a daily commit.

  Prints one line per change (used as the commit message body).
- `plan --matrix F --manifest F [--pubkey PEM] [--pr-base REF --pr-title T] [--github-output]`:
  build items as JSON (each carries its pin and phantun fields, an `id` such as
  `release-25.12.6-mediatek-filogic` or `snapshot-x86-64`, and `smoke` for `x86/64`).
  - Non-PR: every non-null release pin and (if enabled) snapshot pin whose
    (channel, version, target, kernel, phantun version, pkg_release) is not in the manifest.
    `publish` is true when there are items, the retention dry-run would prune something, or the
    manifest's `pubkey_sha256` is not the sha256 of `--pubkey` (default
    `keys/phantun-openwrt.pem`): key rotation, or a feed published before APKs were signed.
  - PR (`--pr-base`): targets added or with changed pins → newest listed point release of every
    series (+ SNAPSHOT if enabled); recipe or `build-one.sh` changed → every target for the newest
    point release + SNAPSHOT for `mediatek/filogic` and `x86/64`; otherwise nothing. Never
    publishes. Also enforces the `pkg_release` rule.
  - More than 256 items fails.

## Package recipe and `scripts/build-one.sh`

`openwrt/phantun/Makefile.in` is rendered with `sed` (`@PHANTUN_VERSION@`, `@PHANTUN_SHA256@`,
`@PKG_RELEASE@`) into `<sdk>/package/phantun/Makefile`. Never pass these as make variables: a
command-line `PKG_VERSION=` leaks into `package/kernel/linux` and changes `kmod-nf-conntrack`.
Recipe rules: `DEPENDS:=+kmod-nf-conntrack` (also feeds the SDK's dependency check), no
`AUTOLOAD`, no `KCONFIG`; `Build/Prepare` unpacks the tarball, which has no top-level directory;
`Build/Configure` runs the shipped `configure` with `ARCH`/`CROSS_COMPILE` exported so the probes
use the target toolchain.

`build-one.sh --item-json '<plan item>' --out DIR [--work DIR] [--cache DIR]` (or one flag per
field; see the script header). Steps:

1. Validate every input strictly; refuse root; work dir without whitespace.
2. Download the SDK (release: downloads, then archive), verify the pinned sha256. SNAPSHOT
   mismatch or 404 → exit 3 ("stale"); release mismatch → failure. `--cache` keeps verified
   SDK/source tarballs between local runs.
3. Extract (`tar --zstd --strip-components=1`), delete the tarball.
4. Identity: `include/kernel.mk` fallback hash == pinned vermagic (SNAPSHOT mismatch → exit 3),
   single `build_dir/target-*/linux-*/linux-<v>` with `<v>` == pinned linux version.
5. Fetch and verify `phantun-dkms_<v>.tar.gz` into `sdk/dl/`, render the recipe.
6. Trim prompt-less kmod defaults from `Config-build.in`, write a minimal `.config`
   (`CONFIG_PACKAGE_kmod-phantun=m`, `CONFIG_ALL_KMODS` and `CONFIG_AUTOREMOVE` unset),
   `make defconfig`. No `scripts/feeds`, no `NO_DEPS=1`.
7. `make package/phantun/compile -j$(nproc) V=s` → `compile.log`.
8. Checks: exactly one `kmod-phantun-<apkver>.apk`; `apk adbdump` shows name, version, arch and
   depends exactly `kernel=<v>~<vermagic>-r<rel>` + `kmod-nf-conntrack`, and contains
   `lib/modules/<v>/phantun.ko`; `compile.log` has no `undefined!` and no `WARNING: modpost:`
   other than `missing MODULE_DESCRIPTION() in phantun.o` (expected on ≥ 6.18:
   `CONFIG_MODULE_STRIPPED` compiles the description away).
9. `--out`: the APK, `build.json` (item, APK sha256/size, packaged `phantun.ko` sha256, times,
   run URL), `config.h`, `config.log`; `compile.log.tail` on failure.

Never publish anything else from the SDK's `bin/` (it repackages `kernel`/`kmod-nf-conntrack` and
signs with a throwaway `private-key.pem`). A build takes ~30 s after the SDK download.

Reference results (useful to detect pipeline drift): 25.12.6 `mediatek/filogic` with phantun-dkms
0.6.0 → packaged `phantun.ko` sha256 `893bf0f13d2203c7f8f63b6d9725b61315be4abf38a14e800bd70c1b5576ec73`;
with 0.6.0.1 → APK sha256 `053f72f23fa36ab6ce80b16c832416ffd53009652569c3faebf074ff62ec0993`,
`phantun.ko` `e172e0d41f8cf5befd7a397e4dd353ff72087cf9bfb01d9fcd217c39b200175f`.

## `scripts/smoke-x86.py`

For `x86/64` items, after a successful build: downloads the stock
`openwrt-<ver>-x86-64-generic-ext4-combined.img.gz` (verified against GPG-verified `sha256sums`),
signs the APK and indexes it with a throwaway key using the SDK's `staging_dir/host/bin/apk`,
attaches `repo/` + key as a read-only tar disk, boots
`qemu-system-x86_64 [-enable-kvm -cpu host] -m 512 -smp 2 -nographic -no-reboot -nic none` and
drives the console with `pexpect` (prompt `root@[^:\r\n]*:[^#\r\n]*# `): `apk list -I kernel`
must equal the pin, then `apk add --no-network --repository …/packages.adb kmod-phantun`,
`insmod phantun managed_local_ports=51820` (not `modprobe`), `/proc/modules`, parameter readback,
`rmmod`, `apk del` + `apk add --no-network …/<apk>` (standalone, trusted by the APK's own
signature), and no `BUG|Oops|WARNING|Call Trace` in the dmesg lines since `insmod`. SNAPSHOT
image with a different kernel than the pin → "skipped", not failed. ~30 s with KVM.

## Feed (`gh-pages`)

```
index.html, phantun-openwrt.pem, manifest.json, pages-state.json
releases/<ver>/targets/<t>/<s>/kmods/<kernel-id>/{packages.adb,kmod-phantun-<apkver>.apk}
snapshots/targets/<t>/<s>/kmods/<kernel-id>/{packages.adb,kmod-phantun-<apkver>.apk}
```

The path suffix equals the router's own `distfeeds.list` kmods line, which is what the README's
`sed` relies on. Each kmods directory holds exactly one signed APK and its signed index.

`manifest.json` (schema 2) = `{"schema", "pubkey_sha256" (sha256 of the PEM everything is signed
for), "entries"}` with one entry per APK: `channel`, `series` (releases), `version`, `target`,
`kernel`, `arch`, `version_code`, `phantun_version`, `pkg_release`, `path`, `apk_sha256` and
`apk_size` (the published, signed file), `unsigned_sha256` (the build output, reproducible: the
published file minus signatures, `apk adbsign --allow-untrusted --reset-signatures`), `built_at`,
`run_url`. It is the record of what is built (`plan` diffs against it) and, with the orphan
`gh-pages`, the audit trail.

`pages-state.json` = `{"content_sha256": sha256 over sorted "<path>\0<file sha256>\n" of every
other file}`: the deploy job's "is the live site current?" token.

### `scripts/publish.py`

Inputs: `matrix.json`, the current tree (exported from `gh-pages`), the plan items and the build
artifacts (`item.json`, `status`, and on success `build.json` + APK). Steps:

1. Abort if any **release** item of the run is not `ok` (release publishing is all-or-nothing).
2. Sign every `ok` APK (sha256 checked against `build.json`, item checked against the plan) into
   place with `apk adbsign --allow-untrusted --reset-signatures --sign-key`; an entry with the same
   key is replaced. `adbsign` exits 0 even when it fails and may leave a truncated file, so the
   result is checked instead: `apk --root <tmp> verify` with only our public key in
   `<tmp>/etc/apk/keys/`, and the file minus signatures must equal the `build.json` sha256.
3. **Retention** (a storage cap, never a reason to build): delete release entries whose
   (phantun version, pkg_release) differs from the matrix, whose series is no longer in
   `matrix.releases`, or whose target is no longer listed; then per series keep only the newest
   `retain_point_releases` point releases present in the feed (point releases outside the build
   window stay published under this cap).
4. SNAPSHOT: everything removed if `policy.snapshot` is false; otherwise keep entries of listed
   targets with the current phantun, newest 5 per target by `version_code` number.
5. If the manifest's `pubkey_sha256` differs from the public key (rotation, or a schema-1 feed with
   unsigned APKs), re-sign every kept APK the same way (a schema-1 `apk_sha256` is the unsigned
   hash).
6. Remove emptied directories; re-sign `packages.adb` **only** in directories whose APK set changed
   (ECDSA signatures are randomized; re-signing everything would change the tree on every run), or
   in every directory after step 5. The index is built from the signed APKs (it records their
   size) and checked with `apk --root <tmp> verify`.
7. Write `manifest.json`, `index.html`, `phantun-openwrt.pem`, `pages-state.json` —
   deterministic (no timestamps), so an unchanged input gives a byte-identical tree.
8. Sanity: APKs on disk == manifest (paths and sha256), exactly one APK + index per kmods
   directory, no stray files, total < 500 MB.

`apk` here is apk-tools v3.0.5 (commit `b5a31c0d865342ad80be10d68f1bb3d3ad9b0866`, the version
OpenWrt 25.12.6 uses) built from source with meson and cached by commit; it is cloned from the
GitHub mirror `alpinelinux/apk-tools` because gitlab.alpinelinux.org rejects GitHub runners
(HTTP 418). Ubuntu's `apk-tools` is v2 and has no `mkndx`.

## CI (`.github/workflows/ci.yml`)

| Event | Effect |
|---|---|
| `schedule` 04:23 UTC daily | `bump`, build, publish, deploy |
| `workflow_dispatch` (`bump`, `force_deploy`) | `bump=false` runs `fill` only; `force_deploy` redeploys gh-pages |
| `push` to `master` touching `matrix.json`, `openwrt/**`, `scripts/**`, `keys/**`, the workflow | `fill`, build missing, publish, deploy |
| `pull_request` (same paths) | validate and test-build, never publish |

```mermaid
flowchart LR
  bump["bump (not PR)<br/>bump or fill → validate --complete<br/>commit matrix.json to master"] --> plan
  plan["plan<br/>items = desired − manifest<br/>PR: structure → fill --in-memory → complete → plan --pr-base"] --> build
  build["build (matrix, fail-fast off, ≤20 parallel)<br/>build-one.sh (+ smoke-x86.py)<br/>artifact build-&lt;id&gt;"] --> publish
  publish["publish (not PR)<br/>publish.py → orphan commit → force-push gh-pages"] --> deploy
  deploy["deploy (always, not PR)<br/>live pages-state.json ≠ gh-pages? → deploy-pages → verify"] --> report
  report["report (always)<br/>summary; fail on failed jobs / snapshot items"]
```

- Pushes made with `GITHUB_TOKEN` (bump commit, gh-pages) do not trigger workflows: no loops;
  the bump commit and its build happen in the same run.
- Failure policy: any failed **release** item fails `build`, so `publish` does not run and Pages is
  unchanged; the next run retries everything missing. SNAPSHOT items never fail `build`: their step
  records `stale`/`failed` in the artifact and exits 0; `publish` takes the `ok` ones and `report`
  fails the run if any SNAPSHOT item `failed` (stale is only a warning).
- Deploy is decoupled from publishing: it runs on every non-PR run, compares the live
  `pages-state.json` (cache-busting query, `Cache-Control: no-cache`) with `gh-pages` HEAD and
  deploys `git archive gh-pages` if they differ (or `force_deploy`), then polls up to 10 min
  until the live file matches. A failed or cancelled deployment therefore heals on the next run.
  The `github-pages` environment only allows `master`.
- Build volume: only `desired − manifest`; nothing published is rebuilt. A new point release
  builds once per target; a phantun-dkms release or `pkg_release` change rebuilds
  `build_point_releases` × targets × series + one SNAPSHOT per target; a new SNAPSHOT kernel
  builds that target once. There is no "rebuild all" switch.
- Secrets: `APK_SIGNING_KEY` (EC P-256 private key PEM) is only exposed to `publish`, never to
  build jobs (which run SDK and phantun-dkms code). Fork PRs get no secrets and never publish; never
  use `pull_request_target` with a PR checkout.
- Third-party actions are pinned by commit SHA.

## Setup (done once)

- Repository public (free runners); default branch `master`; Pages source "GitHub Actions";
  `master` unprotected or with a bypass for the bump push; `gh-pages` must allow force pushes.
- Signing key:
  `openssl ecparam -name prime256v1 -genkey -noout -out phantun-openwrt.key` and
  `openssl ec -in phantun-openwrt.key -pubout -out keys/phantun-openwrt.pem`; private key into the
  `APK_SIGNING_KEY` secret plus an offline backup, never committed; publish the PEM's sha256 in
  the README. Rotation: update the `APK_SIGNING_KEY` secret and commit the new public key; the
  push run's `plan` sees the manifest's `pubkey_sha256` differ and `publish` re-signs every APK
  and index. Tell users to install the new key.
- OpenWrt key: compare the fingerprint of `keys/openwrt-*.asc` with openwrt.org's signatures page
  before replacing it. When OpenWrt rotates keys, `gpgv` fails and `bump` stops until `keys/` is
  updated.

## Rollout

- **Phase 1**: 25.12.6 × `mediatek/filogic` only, phantun-dkms 0.6.0.1,
  `phantun.hold` and `policy.hold_openwrt` true, `build_point_releases` 1, `snapshot` false.
  Phase 2 starts only after the maintainer has verified the package on real hardware.
- **Phase 2**, each step a green PR + green run before the next:
  1. `hold_openwrt: false`, `build_point_releases: 2`, `phantun.hold: false`; then dispatch with
     `bump=true` (adds 25.12.5: one build).
  2. Targets-only PR adding `x86/64`, `ramips/mt7621`, `ath79/generic`, `qualcommax/ipq807x`,
     `mediatek/mt7622`, `ipq40xx/generic`, `qualcommax/ipq50xx`, `bcm27xx/bcm2712`,
     `rockchip/armv8` (18 builds after merge; first x86/64 smoke test in CI). Next in line:
     `qualcommax/ipq60xx`, `ramips/mt7620`, `ramips/mt76x8`, `bcm27xx/bcm2711`,
     `ipq40xx/chromium`, `armsr/armv8`.
  3. `policy.snapshot: true`. Requires a stable phantun-dkms release containing commit `bfaa751`
     (`timer_container_of` probe fix for `-Werror` kernels ≥ 6.16); 0.6.0.1 contains it.

## Local development

Requirements: Linux x86_64, non-root, Python 3, `gpgv`, `openssl`, `curl`, `zstd`, and the OpenWrt
SDK host tools (Ubuntu: `build-essential gawk gettext libncurses-dev python3 rsync unzip wget file
zstd bzip2 perl`); `qemu-system-x86` + `python3-pexpect` for the smoke test; `meson ninja-build
libssl-dev zlib1g-dev pkg-config` to build apk-tools. `work/` and `out/` are git-ignored.

A CI-like environment:

```sh
cat > Dockerfile <<'EOF'
FROM ubuntu:24.04
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    ca-certificates build-essential gawk gettext libncurses-dev python3 rsync unzip wget file zstd \
    bzip2 perl curl git gpgv openssl qemu-system-x86 python3-pexpect meson ninja-build \
    libssl-dev zlib1g-dev pkg-config && rm -rf /var/lib/apt/lists/*
RUN userdel -r ubuntu; useradd -m -u 1000 runner
USER runner
EOF
docker build -t phantun-openwrt-ci .
docker run --rm -v "$PWD:$PWD" -w "$PWD" phantun-openwrt-ci scripts/build-one.sh …
```

Typical checks:

```sh
python3 scripts/matrix.py validate --structure
python3 scripts/matrix.py fill --in-memory --out work/filled.json
python3 scripts/matrix.py validate --complete --matrix work/filled.json
python3 scripts/matrix.py plan --matrix work/filled.json --manifest <(echo '{"entries":[]}')
scripts/build-one.sh --item-json "$(python3 scripts/matrix.py plan --matrix work/filled.json \
    --manifest <(echo '{"entries":[]}') | python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin)["items"][0]))')" \
    --out work/out --work work/build --cache work/cache
python3 scripts/smoke-x86.py --version 25.12.6 --kernel <pin kernel> --apk work/out/kmod-phantun-*.apk \
    --apk-bin work/build/sdk/staging_dir/host/bin/apk --work work/smoke --log work/smoke/console.log
python3 scripts/publish.py --matrix matrix.json --pages work/pages --items items.json \
    --artifacts work/artifacts --signing-key test.key --pubkey test.pem --apk work/apk-tools/bin/apk
actionlint .github/workflows/ci.yml && shellcheck scripts/build-one.sh
```

apk-tools for `publish.py`:

```sh
git clone --depth 1 --branch v3.0.5 https://github.com/alpinelinux/apk-tools.git work/apk-tools-src
cd work/apk-tools-src && meson setup build --prefix="$PWD/../apk-tools" -Ddefault_library=static \
  -Dcrypto_backend=openssl -Ddocs=disabled -Dhelp=disabled -Dlua=disabled -Dpython=disabled \
  -Dtests=disabled -Dzstd=disabled && ninja -C build && meson install -C build
```

Synthetic APKs for publish tests: `apk mkpkg --info name:kmod-phantun --info version:<v>
--info arch:<arch> --info "depends:kernel=<v>~<hash>-r1 kmod-nf-conntrack" --files <dir>
--output <file>.apk`.

## Caveats

SDK pitfalls:

- `Config-build.in` turns the buildbot's package selection into prompt-less `default m/y`;
  `make defconfig` re-enables ~1080 kmods regardless of `.config`. Without the trim (step 6) the
  build first recompiles all kmods (~9 min instead of ~30 s); the resulting `phantun.ko` is
  identical either way.
- `NO_DEPS=1` breaks the dependency check (`.provides` never generated). Don't run
  `scripts/feeds update` (slow, network, unnecessary).
- `openwrt/gh-action-sdk` and `ghcr.io/openwrt/sdk:<arch>-<ver>` arch-alias tags map to an arbitrary
  target's SDK: fine for userspace, wrong for kmods. Use the per-target tarballs.
- `make defconfig` prints Kconfig warnings and an `openssl.cnf` message: harmless.
- `configure` probes see only `ARCH`/`CROSS_COMPILE` plus the kernel's `CONFIG_WERROR`; the real
  build uses OpenWrt's `KERNEL_MAKE_FLAGS`.
- SNAPSHOT SDK names carry the GCC version: always take the file name from `sha256sums`.
- SDK host tools are prebuilt by OpenWrt's buildbot; a future SDK needing a newer host glibc would
  break builds loudly.

Upstream timing:

- `.versions.json` is unsigned and may list a version before all targets are uploaded (handled by
  the completeness rule). SNAPSHOT `profiles.json`, `sha256sums`, the SDK and the x86 image can
  come from different buildbot runs; pins go stale (exit 3, smoke "skipped") and the next daily
  run catches up. A SNAPSHOT identity whose SDK was replaced before we built it is lost.
- After a phantun-dkms bump, SNAPSHOT coverage restarts at one identity per target and old point
  releases outside the build window lose their packages (accepted to bound runner usage).
- A target can disappear or be renamed in a new series (pin `null`); update `targets` by hand.
- Upstream re-spins of a release (sha256 changed) fail validation instead of being re-pinned.
- phantun-dkms's own CI publishes GitHub releases as pre-releases; `bump` only sees a release once
  the maintainer marks it stable. A replaced asset with the same version is an error.

phantun-dkms compatibility:

- Every `configure` probe must compile warning-free under `-Werror`, use what it defines, and log
  to `config.log`; v0.6.0's `timer_container_of` probe did not and broke on kernels ≥ 6.16
  (fixed in `bfaa751`, released in 0.6.0.1).
- Supported kernels 5.10–7.2; a newer SNAPSHOT kernel may fail: fix upstream, use
  `snapshot_exclude` meanwhile.
- 32-bit MIPS targets built without 64-bit division helpers; the `undefined!` check guards a
  regression.

Runtime on the router:

- OpenWrt's `modprobe` (ubox) does not pass parameters; `insmod phantun <params>` does.
- Default `ip_families=both` registers IPv4 and IPv6 hooks; stock kernels have `CONFIG_IPV6=y`.
- `CONFIG_MODULE_STRIPPED` removes descriptions; `modinfo phantun` shows little.

GitHub platform:

- 256 jobs per matrix, 20 concurrent jobs (Free), 6 h per job; standard runners are free for
  public repos.
- Scheduled workflows are disabled after 60 days without repository activity; re-enable if the
  repository goes quiet. Failure emails go to whoever last edited the cron line.
- `GITHUB_TOKEN` cannot modify `.github/workflows/*` (bump never does). The bump push can race a
  human push: one `pull --rebase`, then fail; the next run retries.
- Pages: ~10 min CDN caching (APK names are versioned, so a retry fixes a stale edge), 1 GB site
  limit, 100 GB/month soft bandwidth.
