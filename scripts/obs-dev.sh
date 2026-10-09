#!/bin/bash
# Runs OBS Studio on macOS with this checkout's build of the face tracker and
# obs-ptz both loaded, without installing either into your OBS profile.
# OBS is pointed at a folder of the two plugin bundles through
# OBS_PLUGINS_PATH and OBS_PLUGINS_DATA_PATH.
#
# Usage: scripts/obs-dev.sh [setup|run|all|link] [--clean-profile]
#   setup  make the folder of plugins from the builds
#   run    start OBS with it
#   all    both, which is the default
#   link   instead of those: make a bundle of the build in build/, with
#          links to what is in it, and link that from OBS's own plugins
#          folder, as obs-ptz can be. A rebuild is then what OBS loads next,
#          with nothing to copy.
#   --clean-profile  start OBS with an empty profile, in a temporary home,
#                    which shows its first-run wizard
#
# The face tracker has to be built already, and so does obs-ptz.
#
# Env overrides:
#   FT_BUILD_DIR     where obs-face-tracker.so was built (default: build/)
#   OBS_PTZ_PLUGIN   the obs-ptz.plugin to load (default: that of an obs-ptz
#                    checkout beside this one, in build_macos/rundir/RelWithDebInfo)
#   OBS_APP          the OBS app bundle (default: the newest "/Applications/OBS <version>.app"
#                    that is not a beta)
#   OBS_DEV_DIR      where the folder of plugins is made
#                    (default: ~/Library/Caches/obs-face-tracker-dev)

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="${FT_BUILD_DIR:-$ROOT/build}"
# obs-ptz is expected beside this checkout, or beside the main one if this is a worktree
if [ -z "${OBS_PTZ_PLUGIN:-}" ]; then
	main="$(dirname "$(git -C "$ROOT" rev-parse --path-format=absolute --git-common-dir 2>/dev/null || echo "$ROOT/.git")")"
	for dir in "$ROOT/.." "$main/.."; do
		OBS_PTZ_PLUGIN="$dir/obs-ptz/build_macos/rundir/RelWithDebInfo/obs-ptz.plugin"
		[ -d "$OBS_PTZ_PLUGIN" ] && break
	done
fi
DEV_DIR="${OBS_DEV_DIR:-$HOME/Library/Caches/obs-face-tracker-dev}"
PLUGINS="$DEV_DIR/plugins"
MODELS="$DEV_DIR/models"
HOG_URL="https://github.com/norihiro/obs-face-tracker/releases/download/0.7.0-hogdata/frontal_face_detector.dat.bz2"

die() {
	echo "error: $*" >&2
	exit 1
}

mode=all
clean=0
for arg in "$@"; do
	case "$arg" in
	setup | run | all | link) mode="$arg" ;;
	--clean-profile) clean=1 ;;
	*) die "unknown argument '$arg' (see the top of this script)" ;;
	esac
done

[ "$(uname)" = Darwin ] || die "this script is for macOS"

# The detector's model is not in the repository
fetch_model() {
	if [ ! -s "$MODELS/dlib_hog_model/frontal_face_detector.dat" ]; then
		echo "==> Downloading the face detector model"
		mkdir -p "$MODELS/dlib_hog_model"
		curl -fsSL "$HOG_URL" | bunzip2 >"$MODELS/dlib_hog_model/frontal_face_detector.dat.part"
		mv "$MODELS/dlib_hog_model/frontal_face_detector.dat.part" "$MODELS/dlib_hog_model/frontal_face_detector.dat"
	fi
}

link() {
	local lib="$BUILD_DIR/obs-face-tracker.so"
	[ -f "$lib" ] || die "no $lib: build the face tracker first, or set FT_BUILD_DIR"
	local dest="$HOME/Library/Application Support/obs-studio/plugins/obs-face-tracker.plugin"
	if [ -e "$dest" ] && [ ! -L "$dest" ]; then
		die "$dest is a copy, not a link: remove it first"
	fi
	fetch_model

	# The library the build makes is signed by the linker, which is enough, so
	# nothing here is signed or copied: it all links to where it is
	local bundle="$BUILD_DIR/obs-face-tracker.plugin"
	echo "==> Making $bundle"
	rm -rf "$bundle"
	mkdir -p "$bundle/Contents/MacOS" "$bundle/Contents/Resources"
	ln -s ../../../obs-face-tracker.so "$bundle/Contents/MacOS/obs-face-tracker"
	ln -s "$ROOT/data/locale" "$bundle/Contents/Resources/locale"
	ln -s "$MODELS/dlib_hog_model" "$bundle/Contents/Resources/dlib_hog_model"
	cat >"$bundle/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0"><dict>
<key>CFBundleExecutable</key><string>obs-face-tracker</string>
<key>CFBundleIdentifier</key><string>dev.obs-face-tracker</string>
<key>CFBundlePackageType</key><string>BNDL</string>
</dict></plist>
PLIST

	mkdir -p "$(dirname "$dest")"
	ln -sfn "$(cd "$bundle" && pwd -P)" "$dest"
	echo "==> $dest -> $bundle"
}

setup() {
	local lib="$BUILD_DIR/obs-face-tracker.so"
	[ -f "$lib" ] || die "no $lib: build the face tracker first, or set FT_BUILD_DIR"
	[ -d "$OBS_PTZ_PLUGIN" ] || die "no obs-ptz at $OBS_PTZ_PLUGIN: build it, or set OBS_PTZ_PLUGIN"
	fetch_model

	echo "==> Making $PLUGINS"
	local bundle="$PLUGINS/obs-face-tracker.plugin"
	rm -rf "$bundle"
	mkdir -p "$bundle/Contents/MacOS" "$bundle/Contents/Resources"
	cp "$lib" "$bundle/Contents/MacOS/obs-face-tracker"
	cp -R "$ROOT/data/." "$bundle/Contents/Resources/"
	cp -R "$MODELS/." "$bundle/Contents/Resources/"
	cat >"$bundle/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0"><dict>
<key>CFBundleExecutable</key><string>obs-face-tracker</string>
<key>CFBundleIdentifier</key><string>dev.obs-face-tracker</string>
<key>CFBundlePackageType</key><string>BNDL</string>
</dict></plist>
PLIST
	# Ad hoc, which is enough for OBS to load it
	codesign --force --sign - "$bundle" >/dev/null 2>&1

	ln -sfn "$(cd "$OBS_PTZ_PLUGIN" && pwd -P)" "$PLUGINS/obs-ptz.plugin"
}

run() {
	[ -d "$PLUGINS/obs-face-tracker.plugin" ] || die "no plugins in $PLUGINS: run setup first"

	# The newest release, not a beta or a release candidate
	local app="${OBS_APP:-$(ls -d /Applications/OBS\ [0-9]*.app 2>/dev/null | grep -E '/OBS [0-9]+(\.[0-9]+)*\.app$' | sort -V | tail -1)}"
	local binary="$app/Contents/MacOS/OBS"
	[ -x "$binary" ] || die "OBS not found at '$binary' (set OBS_APP to override)"

	# A second OBS asks whether to start anyway, in a dialog
	if pgrep -x OBS >/dev/null 2>&1; then
		echo "warning: OBS is already running, and will ask about a second one" >&2
	fi

	local env_home=()
	if [ "$clean" = 1 ]; then
		local home
		home="$(mktemp -d)"
		echo "==> Empty profile in $home"
		env_home=("CFFIXED_USER_HOME=$home")
	fi

	echo "==> Starting $binary"
	exec env "${env_home[@]+"${env_home[@]}"}" \
		OBS_PLUGINS_PATH="$PLUGINS" OBS_PLUGINS_DATA_PATH="$PLUGINS" \
		"$binary" --disable-updater
}

case "$mode" in
link) link ;;
setup) setup ;;
run) run ;;
all)
	setup
	run
	;;
esac
