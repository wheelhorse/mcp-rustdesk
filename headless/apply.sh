#!/usr/bin/env bash
# Clone RustDesk, apply our patches, and build the `rustdesk-headless` binary.
#
# Works on Linux and macOS. For Windows, see `headless/apply.ps1`.
#
# Usage:
#   ./headless/apply.sh [target-dir]
#
# Defaults to ./rustdesk in the repo root. Re-running is safe — patches are
# idempotent and the script skips steps already done.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"
TARGET="${1:-$REPO_ROOT/rustdesk}"

RUSTDESK_REPO="${RUSTDESK_REPO:-https://github.com/rustdesk/rustdesk.git}"
RUSTDESK_REF="${RUSTDESK_REF:-master}"
VCPKG_DIR="${VCPKG_DIR:-$REPO_ROOT/vcpkg}"

OS="$(uname -s)"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }

bold "==> Host OS: $OS"

bold "==> Checking host build prerequisites"
need=()
for cmd in git cargo cmake nasm yasm clang pkg-config; do
  command -v "$cmd" >/dev/null 2>&1 || need+=("$cmd")
done
if (( ${#need[@]} > 0 )); then
  case "$OS" in
    Linux)
      cat <<EOF
Missing build tools: ${need[*]}
Install on Debian/Ubuntu:
  sudo apt-get install -y nasm yasm clang cmake pkg-config \\
    libasound2-dev libpulse-dev libgtk-3-dev libxdo-dev libxfixes-dev \\
    libxcb-randr0-dev libxcb-shape0-dev libxcb-xfixes0-dev libpam0g-dev \\
    libva-dev libvdpau-dev libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev \\
    curl zip unzip tar git

Install Rust via https://rustup.rs/ if missing.
EOF
      ;;
    Darwin)
      cat <<EOF
Missing build tools: ${need[*]}
Install on macOS (Homebrew):
  xcode-select --install                  # if not already installed
  brew install nasm yasm cmake pkg-config llvm

Install Rust via https://rustup.rs/ if missing.
EOF
      ;;
    *)
      echo "Unsupported OS: $OS. Missing: ${need[*]}"
      ;;
  esac
  exit 1
fi

bold "==> Cloning RustDesk into $TARGET"
# A CI cache may have restored rustdesk/target/ (creating rustdesk/ as a
# side effect) before we get here; in that case `git clone` aborts on a
# non-empty destination. Move target/ aside, wipe, clone, then restore.
saved_target=""
if [[ -d "$TARGET" && ! -d "$TARGET/.git" ]]; then
  if [[ -d "$TARGET/target" ]]; then
    saved_target="${TARGET%/}.target.saved"
    rm -rf "$saved_target"
    mv "$TARGET/target" "$saved_target"
  fi
  rm -rf "$TARGET"
fi
if [[ ! -d "$TARGET/.git" ]]; then
  git clone --depth 1 --branch "$RUSTDESK_REF" "$RUSTDESK_REPO" "$TARGET"
fi
if [[ -n "$saved_target" && -d "$saved_target" ]]; then
  mv "$saved_target" "$TARGET/target"
fi
git -C "$TARGET" submodule update --init --recursive

bold "==> Patching src/lib.rs (module visibility)"
if grep -q '^pub mod ui_session_interface;' "$TARGET/src/lib.rs"; then
  echo "  already patched"
else
  patch -p1 -d "$TARGET" < "$HERE/lib.rs.patch"
fi

bold "==> Adding [[bin]] rustdesk-headless to Cargo.toml"
if grep -q 'name = "rustdesk-headless"' "$TARGET/Cargo.toml"; then
  echo "  already added"
else
  awk '
    /^\[\[bin\]\]$/ { count++ }
    { print }
    count == 2 && /^path = "src\/service.rs"$/ {
      print ""
      print "[[bin]]"
      print "name = \"rustdesk-headless\""
      print "path = \"src/bin/headless.rs\""
      print "required-features = []"
      count = 99
    }
  ' "$TARGET/Cargo.toml" > "$TARGET/Cargo.toml.new"
  mv "$TARGET/Cargo.toml.new" "$TARGET/Cargo.toml"
fi

bold "==> Copying headless.rs into the RustDesk tree"
mkdir -p "$TARGET/src/bin"
cp "$HERE/headless.rs" "$TARGET/src/bin/headless.rs"

bold "==> Bootstrapping vcpkg + codecs (one-time, ~30–60 min)"
if [[ ! -x "$VCPKG_DIR/vcpkg" ]]; then
  git clone https://github.com/microsoft/vcpkg.git "$VCPKG_DIR"
  "$VCPKG_DIR/bootstrap-vcpkg.sh" -disableMetrics
fi
export VCPKG_ROOT="$VCPKG_DIR"
"$VCPKG_DIR/vcpkg" install libvpx libyuv opus aom

bold "==> cargo build --release --bin rustdesk-headless"
( cd "$TARGET" && VCPKG_ROOT="$VCPKG_DIR" CXXFLAGS="-include cstdint" cargo build --release --bin rustdesk-headless )

bin="$TARGET/target/release/rustdesk-headless"
bold "==> Done. Binary: $bin"
ls -la "$bin"
