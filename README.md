# phantun-dkms-openwrt

Ready-to-install `kmod-phantun` packages for **stock, unmodified OpenWrt** routers.

[phantun-dkms](https://github.com/bjin/phantun-dkms) is a Linux kernel module that carries
selected UDP flows (for example WireGuard) as fake TCP, without a TUN device or a userspace
daemon. OpenWrt does not ship it, and a kernel module has to be built for the exact kernel of the
firmware it runs on. This repository builds it for official OpenWrt releases and publishes the
packages as signed `apk` feeds at <https://bjin.github.io/phantun-dkms-openwrt/>, so a router can
install it with plain `apk add kmod-phantun`.

## What is available

- OpenWrt **25.12 and later** (apk-based releases) and, later, main **SNAPSHOT**. OpenWrt 24.10 and
  older (opkg/ipk) are not supported.
- A fixed list of targets. **Right now only OpenWrt 25.12.6 `mediatek/filogic`** is published
  while the first package is tested on real hardware; more targets and versions follow.

The web page lists every published package; `manifest.json` next to it is the machine-readable
list.

## Install on a router

```sh
BASE=https://bjin.github.io/phantun-dkms-openwrt
uclient-fetch -O /etc/apk/keys/phantun-openwrt.pem "$BASE/phantun-openwrt.pem"
sha256sum /etc/apk/keys/phantun-openwrt.pem   # must match the fingerprint below
sed -nE "s#^.*/((releases|snapshots)/.*/kmods/[^/]+/packages\.adb)\$#$BASE/\1#p" \
    /etc/apk/repositories.d/distfeeds.list > /etc/apk/repositories.d/phantun.list
apk update && apk add kmod-phantun
insmod phantun managed_local_ports=51820        # parameters: see the phantun-dkms README
```

Signing key fingerprint (SHA-256 of `phantun-openwrt.pem`):

```
bb3c11505fca15509c79d3986b52d05d1b78b509669173081089729f94d3f1f4
```

The `sed` line turns your router's own official kernel-module feed line into the matching feed URL
here, so the router only ever sees the package built for its exact firmware.

### Good to know

- **Not built for your router?** `apk update` reports a fetch error for `phantun.list` and
  `apk add` finds no `kmod-phantun`. See "Requesting a target" below.
- **Loading:** installing never loads the module. Use `insmod phantun <parameters>`;
  OpenWrt's `modprobe` does not pass parameters to it. To load at boot, put a line
  `phantun <parameters>` into `/etc/modules.d/99-phantun`.
- **Firmware upgrades** change the kernel: after upgrading, run the `sed` line again and reinstall
  with `apk update && apk add kmod-phantun`. Add `/etc/apk/keys/phantun-openwrt.pem` to
  `/etc/sysupgrade.conf` so the key survives upgrades.
- **Attended Sysupgrade / `owut`** cannot build firmware images that contain `kmod-phantun`:
  use `owut upgrade --remove kmod-phantun`, then reinstall after the upgrade.
- **Never** use `--force-broken-world` or `--allow-untrusted`, and never copy `phantun.ko` between
  routers: OpenWrt kernels do not reject modules built for another kernel, and a wrong module can
  crash the router. The package's dependency on the exact `kernel` package is what prevents this.
- **Remove:** `rmmod phantun; apk del kmod-phantun`.

### How long packages stay available

A stable point release (e.g. 25.12.6) stays in the feed while it is among the newest five point
releases of its series. When a new phantun-dkms version is released, only the newest two point
releases are rebuilt with it and older ones are removed; the feed fills up again as OpenWrt ships
new point releases. SNAPSHOT keeps the five most recent kernel builds per target. A module you
already installed keeps working either way; you just cannot install it again from the feed.

## How the packages are built

Everything runs in GitHub Actions in this repository; no package is built or signed anywhere else.

1. `matrix.json` lists the OpenWrt versions and targets to build and pins every input: the official
   OpenWrt SDK for each (version, target), identified by its SHA-256 from OpenWrt's GPG-signed
   `sha256sums`, and the phantun-dkms release tarball by its SHA-256.
2. A daily job follows new OpenWrt point releases, SNAPSHOT kernel builds and phantun-dkms stable
   releases, and updates `matrix.json`.
3. Each missing package is built with the unmodified official SDK, checked (package name, version,
   architecture, exact `kernel` dependency, no unresolved symbols) and, for `x86/64`, installed and
   loaded in a stock OpenWrt virtual machine.
4. The feed indexes are signed with this repository's key and published to GitHub Pages.

Builds are reproducible: the same inputs give a byte-identical package, so you can rebuild one
yourself and compare it with the published file (see `DEVELOPMENT.md`).

## Requesting a target

Add the `target/subtarget` (as in the official download URL, e.g. `qualcommax/ipq60xx`) to the
sorted `targets` list in `matrix.json` and open a pull request; nothing else is needed. CI
test-builds it and, after merge, publishes it. Or open an issue naming your device and target.

## Documentation

- `DEVELOPMENT.md`: design, build pipeline, CI, local development.
- `AGENTS.md`: notes for coding agents working on this repository.
