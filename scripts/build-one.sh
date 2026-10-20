#!/usr/bin/env bash
# Build kmod-phantun for one plan item with the official OpenWrt SDK (DEVELOPMENT.md, "build-one.sh").
#
# Usage: scripts/build-one.sh --item-json '<plan item JSON>' --out <dir> [--work <dir>] [--cache <dir>]
#        scripts/build-one.sh --channel release|snapshot --version <25.12.6|SNAPSHOT> --target <t/s> \
#            --sdk <file> --sdk-sha256 <hex> --kernel <linux-release-vermagic> --arch <arch> \
#            --version-code <rNNNNN-hash> --phantun-version <v> --phantun-sha256 <hex> \
#            --pkg-release <n> --out <dir> [--work <dir>] [--cache <dir>]
#
# --work  scratch directory (default ./work); wiped first, must not contain spaces.
# --cache optional directory holding (and receiving) the SDK tarball and phantun-dkms source
#         tarball; files are always re-verified against the pinned sha256.
#
# Exit status: 0 success, 3 stale snapshot pin (SDK replaced upstream), anything else failure.
set -euo pipefail

die() { echo "build-one: error: $*" >&2; exit 1; }
stale() { echo "build-one: stale: $*" >&2; exit 3; }
log() { echo "build-one: $*" >&2; }

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

channel='' version='' target='' sdk='' sdk_sha256='' kernel='' arch='' version_code=''
phantun_version='' phantun_sha256='' pkg_release='' out='' work="$PWD/work" cache=''

load_item_json() {
	local assignments
	assignments=$(python3 - "$1" <<'EOF'
import json, shlex, sys
item = json.loads(sys.argv[1])
fields = {"channel": "channel", "version": "version", "target": "target", "sdk": "sdk",
          "sdk_sha256": "sdk_sha256", "kernel": "kernel", "arch": "arch",
          "version_code": "version_code", "phantun_version": "phantun_version",
          "phantun_sha256": "phantun_sha256", "pkg_release": "pkg_release"}
for key, var in fields.items():
    if key not in item:
        sys.exit(f"item JSON lacks {key!r}")
    print(f"{var}={shlex.quote(str(item[key]))}")
EOF
	) || die "cannot parse --item-json"
	eval "$assignments"
}

while [[ $# -gt 0 ]]; do
	case $1 in
	--item-json) load_item_json "$2"; shift 2 ;;
	--channel) channel=$2; shift 2 ;;
	--version) version=$2; shift 2 ;;
	--target) target=$2; shift 2 ;;
	--sdk) sdk=$2; shift 2 ;;
	--sdk-sha256) sdk_sha256=$2; shift 2 ;;
	--kernel) kernel=$2; shift 2 ;;
	--arch) arch=$2; shift 2 ;;
	--version-code) version_code=$2; shift 2 ;;
	--phantun-version) phantun_version=$2; shift 2 ;;
	--phantun-sha256) phantun_sha256=$2; shift 2 ;;
	--pkg-release) pkg_release=$2; shift 2 ;;
	--out) out=$2; shift 2 ;;
	--work) work=$2; shift 2 ;;
	--cache) cache=$2; shift 2 ;;
	*) die "unknown argument: $1" ;;
	esac
done

# Everything below ends up in URLs, paths, sed expressions or make variables: validate strictly.
[[ $channel =~ ^(release|snapshot)$ ]] || die "bad --channel '$channel'"
if [[ $channel == release ]]; then
	[[ $version =~ ^[0-9]{2}\.[0-9]{2}\.[0-9]+$ ]] || die "bad release --version '$version'"
else
	[[ $version == SNAPSHOT ]] || die "snapshot --version must be SNAPSHOT"
fi
[[ $target =~ ^[a-z0-9_-]+/[a-z0-9_-]+$ ]] || die "bad --target '$target'"
[[ $sdk =~ ^openwrt-sdk-[A-Za-z0-9._+-]+\.Linux-x86_64\.tar\.zst$ ]] || die "bad --sdk '$sdk'"
[[ $sdk_sha256 =~ ^[0-9a-f]{64}$ ]] || die "bad --sdk-sha256"
[[ $kernel =~ ^([0-9]+\.[0-9]+\.[0-9]+)-([0-9]+)-([0-9a-f]{32})$ ]] || die "bad --kernel '$kernel'"
linux_version=${BASH_REMATCH[1]} linux_release=${BASH_REMATCH[2]} vermagic=${BASH_REMATCH[3]}
[[ $arch =~ ^[A-Za-z0-9_+-]+$ ]] || die "bad --arch '$arch'"
[[ -n $version_code ]] || die "missing --version-code"
[[ $phantun_version =~ ^[0-9]+(\.[0-9]+)*$ ]] || die "bad --phantun-version '$phantun_version'"
[[ $phantun_sha256 =~ ^[0-9a-f]{64}$ ]] || die "bad --phantun-sha256"
[[ $pkg_release =~ ^[1-9][0-9]*$ ]] || die "bad --pkg-release '$pkg_release'"
[[ -n $out ]] || die "missing --out"

[[ $version_code =~ ^[A-Za-z0-9._+-]+$ ]] || die "bad --version-code '$version_code'"

# Step 1: work dir without spaces, non-root.
[[ $(id -u) != 0 ]] || die "refusing to run as root (the OpenWrt SDK does not support it)"
work=$(realpath -m "$work")
[[ $work != *[[:space:]]* ]] || die "work dir '$work' contains whitespace"
mkdir -p "$out"
out=$(realpath "$out")
if [[ -n $cache ]]; then mkdir -p "$cache"; cache=$(realpath "$cache"); fi

apk_version="$linux_version.$phantun_version-r$pkg_release"
kernel_dep="kernel=$linux_version~$vermagic-r$linux_release"
tgt=${target%/*} sub=${target#*/}
if [[ $channel == release ]]; then
	bases=("https://downloads.openwrt.org/releases/$version/targets/$target"
		"https://archive.openwrt.org/releases/$version/targets/$target")
else
	bases=("https://downloads.openwrt.org/snapshots/targets/$target")
fi
started_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
log "item $channel $version $target kernel $kernel -> kmod-phantun $apk_version"

rm -rf "$work"
mkdir -p "$work"
sdkdir="$work/sdk"
compile_log="$work/compile.log"

on_exit() {
	local rc=$?
	if [[ $rc != 0 && -f $compile_log ]]; then
		tail -n 300 "$compile_log" > "$out/compile.log.tail" || true
	fi
}
trap on_exit EXIT

curl_get() { # url dest
	curl -fL --retry 5 --retry-all-errors --retry-delay 5 --connect-timeout 30 -sS -o "$2" "$1"
}

http_status() { # url
	curl -sS -o /dev/null -I -L --retry 5 --retry-delay 5 --connect-timeout 30 -w '%{http_code}' "$1"
}

# Step 2: download and verify the SDK.
sdk_tar="$work/$sdk"
if [[ -n $cache && -f $cache/$sdk ]] && echo "$sdk_sha256  $cache/$sdk" | sha256sum -c --status; then
	log "using cached $cache/$sdk"
	sdk_tar="$cache/$sdk"
else
	got=
	for base in "${bases[@]}"; do
		status=$(http_status "$base/$sdk")
		if [[ $status == 404 ]]; then
			log "$base/$sdk: 404"
			continue
		fi
		log "downloading $base/$sdk"
		curl_get "$base/$sdk" "$sdk_tar"
		got=1
		break
	done
	[[ -n $got ]] || { [[ $channel == snapshot ]] && stale "SDK $sdk no longer exists upstream"; die "SDK $sdk not found upstream"; }
	actual=$(sha256sum "$sdk_tar" | cut -d' ' -f1)
	if [[ $actual != "$sdk_sha256" ]]; then
		[[ $channel == snapshot ]] && stale "SDK sha256 $actual != pinned $sdk_sha256 (replaced upstream)"
		die "SDK sha256 $actual != pinned $sdk_sha256"
	fi
	if [[ -n $cache ]]; then
		mv "$sdk_tar" "$cache/$sdk"
		sdk_tar="$cache/$sdk"
	fi
fi

# Step 3: extract.
mkdir -p "$sdkdir"
tar --zstd -xf "$sdk_tar" -C "$sdkdir" --strip-components=1
[[ $sdk_tar == "$work/$sdk" ]] && rm -f "$sdk_tar"

# Step 4: identity checks.
sdk_vermagic=$(sed -nE 's/^[[:space:]]*LINUX_VERMAGIC:=\$\(if \$\(LINUX_VERMAGIC\),\$\(LINUX_VERMAGIC\),([0-9a-f]{32})\)[[:space:]]*$/\1/p' "$sdkdir/include/kernel.mk")
[[ $(wc -l <<< "$sdk_vermagic") == 1 ]] || die "multiple LINUX_VERMAGIC fallback lines in include/kernel.mk"
[[ -n $sdk_vermagic ]] || die "cannot find the LINUX_VERMAGIC fallback hash in include/kernel.mk"
if [[ $sdk_vermagic != "$vermagic" ]]; then
	[[ $channel == snapshot ]] && stale "SDK vermagic $sdk_vermagic != pinned $vermagic"
	die "SDK vermagic $sdk_vermagic != pinned $vermagic"
fi
shopt -s nullglob
linux_dirs=("$sdkdir"/build_dir/target-*/linux-*/linux-[0-9]*)
shopt -u nullglob
[[ ${#linux_dirs[@]} == 1 ]] || die "expected exactly one build_dir/target-*/linux-*/linux-<v> directory, found ${#linux_dirs[@]}"
sdk_linux_version=${linux_dirs[0]##*/linux-}
[[ $sdk_linux_version == "$linux_version" ]] || die "SDK kernel $sdk_linux_version != pinned $linux_version"
log "SDK identity ok: linux $sdk_linux_version vermagic $sdk_vermagic"

# Step 5: phantun-dkms source and recipe.
src="phantun-dkms_$phantun_version.tar.gz"
mkdir -p "$sdkdir/dl"
if [[ -n $cache && -f $cache/$src ]] && echo "$phantun_sha256  $cache/$src" | sha256sum -c --status; then
	cp "$cache/$src" "$sdkdir/dl/$src"
else
	curl_get "https://github.com/bjin/phantun-dkms/releases/download/v$phantun_version/$src" "$sdkdir/dl/$src"
	[[ -n $cache ]] && cp "$sdkdir/dl/$src" "$cache/$src"
fi
echo "$phantun_sha256  $sdkdir/dl/$src" | sha256sum -c --status ||
	die "$src sha256 $(sha256sum "$sdkdir/dl/$src" | cut -d' ' -f1) != pinned $phantun_sha256"
mkdir -p "$sdkdir/package/phantun"
sed -e "s/@PHANTUN_VERSION@/$phantun_version/g" \
	-e "s/@PHANTUN_SHA256@/$phantun_sha256/g" \
	-e "s/@PKG_RELEASE@/$pkg_release/g" \
	"$REPO_ROOT/openwrt/phantun/Makefile.in" > "$sdkdir/package/phantun/Makefile"
! grep -n '@[A-Z0-9_]*@' "$sdkdir/package/phantun/Makefile" || die "unrendered placeholder in recipe"

# Step 6: drop the prompt-less kmod defaults so that only kmod-phantun and its dependency are built
# (DEVELOPMENT.md, "SDK pitfalls"). CONFIG_AUTOREMOVE is unset so that config.h/config.log survive.
cd "$sdkdir"
awk '/^config PACKAGE_kmod-/{k=1; print; next} /^config /{k=0}
     k && /^\tdefault [ym]$/{next} {print}' Config-build.in > Config-build.in.new
mv Config-build.in.new Config-build.in
printf '%s\n' '# CONFIG_ALL_KMODS is not set' '# CONFIG_AUTOREMOVE is not set' 'CONFIG_PACKAGE_kmod-phantun=m' > .config
make defconfig > "$work/defconfig.log" 2>&1 || { cat "$work/defconfig.log" >&2; die "make defconfig failed"; }
grep -qx 'CONFIG_PACKAGE_kmod-phantun=m' .config || die "kmod-phantun not selected after defconfig"
grep -qx '# CONFIG_AUTOREMOVE is not set' .config || die "CONFIG_AUTOREMOVE still set after defconfig"

# Step 7: compile.
log "compiling (log: $compile_log)"
if ! make package/phantun/compile -j"$(nproc)" V=s > "$compile_log" 2>&1; then
	tail -n 100 "$compile_log" >&2
	die "make package/phantun/compile failed"
fi

# Step 9 (part): keep configure results for the artifact.
shopt -s nullglob
build_dirs=(build_dir/target-*/linux-*/phantun-"$phantun_version" build_dir/target-*/phantun-"$phantun_version")
shopt -u nullglob
[[ ${#build_dirs[@]} == 1 ]] || die "expected exactly one phantun build dir, found ${#build_dirs[@]}"
cp "${build_dirs[0]}/config.h" "${build_dirs[0]}/config.log" "$out/"

# Step 8: post-build checks.
shopt -s nullglob
apks=(bin/targets/"$tgt"/"$sub"/packages/kmod-phantun-*.apk)
shopt -u nullglob
[[ ${#apks[@]} == 1 ]] || die "expected exactly one kmod-phantun APK, found ${#apks[@]}"
apk_file=${apks[0]}
[[ ${apk_file##*/} == "kmod-phantun-$apk_version.apk" ]] || die "unexpected APK name ${apk_file##*/}"

staging_dir/host/bin/apk adbdump --format json "$apk_file" > "$work/adbdump.json"
ko_sha256=$(python3 - "$work/adbdump.json" "$apk_version" "$arch" "$kernel_dep" "$linux_version" <<'EOF'
import json, sys
path, version, arch, kernel_dep, linux_version = sys.argv[1:]
doc = json.load(open(path))
info = doc["info"]
errors = []
def check(what, got, want):
    if got != want:
        errors.append(f"{what}: got {got!r}, want {want!r}")
check("name", info.get("name"), "kmod-phantun")
check("version", info.get("version"), version)
check("arch", info.get("arch"), arch)
check("depends", sorted(info.get("depends", [])), sorted([kernel_dep, "kmod-nf-conntrack"]))
files = {}
for d in doc.get("paths", []):
    for f in d.get("files", []):
        files[f"{d.get('name', '')}/{f['name']}"] = f.get("hash")
want_ko = f"lib/modules/{linux_version}/phantun.ko"
if want_ko not in files:
    errors.append(f"{want_ko} not in package files {sorted(files)}")
if errors:
    sys.exit("APK metadata check failed:\n  " + "\n  ".join(errors))
print(f"build-one: APK metadata ok: {info['name']} {info['version']} {info['arch']} depends {info['depends']}", file=sys.stderr)
print(files[want_ko])
EOF
)
[[ $ko_sha256 =~ ^[0-9a-f]{64}$ ]] || die "no sha256 for phantun.ko in the APK metadata"

! grep -n 'undefined!' "$compile_log" || die "modpost reported undefined symbols"
if bad=$(grep -n 'WARNING: modpost:' "$compile_log" | grep -v 'WARNING: modpost: missing MODULE_DESCRIPTION() in phantun\.o$'); then
	echo "$bad" >&2
	die "unexpected modpost warnings"
fi

# Step 9: outputs.
cp "$apk_file" "$out/"
apk_name=${apk_file##*/}
apk_sha256=$(sha256sum "$apk_file" | cut -d' ' -f1)
apk_size=$(stat -c %s "$apk_file")
run_url=
if [[ -n ${GITHUB_RUN_ID:-} ]]; then
	run_url="${GITHUB_SERVER_URL:-https://github.com}/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}"
fi
python3 - "$out/build.json" "$channel" "$version" "$target" "$sdk" "$sdk_sha256" "$kernel" "$arch" \
	"$version_code" "$phantun_version" "$phantun_sha256" "$pkg_release" "$apk_name" "$apk_version" \
	"$apk_sha256" "$apk_size" "$ko_sha256" "$started_at" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$run_url" <<'EOF'
import json, sys
(path, channel, version, target, sdk, sdk_sha256, kernel, arch, version_code, phantun_version,
 phantun_sha256, pkg_release, apk, apk_version, apk_sha256, apk_size, ko_sha256, started_at,
 built_at, run_url) = sys.argv[1:]
item = {
    "channel": channel, "version": version, "target": target, "sdk": sdk,
    "sdk_sha256": sdk_sha256, "kernel": kernel, "arch": arch, "version_code": version_code,
    "phantun_version": phantun_version, "phantun_sha256": phantun_sha256,
    "pkg_release": int(pkg_release),
}
doc = {
    "item": item, "apk": apk, "apk_version": apk_version, "apk_sha256": apk_sha256,
    "apk_size": int(apk_size), "ko_sha256": ko_sha256, "started_at": started_at,
    "built_at": built_at, "run_url": run_url,
}
with open(path, "w") as f:
    json.dump(doc, f, indent=2, sort_keys=True)
    f.write("\n")
EOF
log "done: $out/$apk_name sha256 $apk_sha256 ($apk_size bytes), phantun.ko sha256 $ko_sha256"
