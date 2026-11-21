#!/bin/bash
# Builds the macOS application bundle and a disk image.
#
#   ./build_mac.sh 1.0.3
#
# Needs the packages from requirements-mac.txt. Set PYTHON to use another interpreter.
# Output: dist/VPN Launcher.app and dist/VpnLauncher-<version>-<arch>.dmg
# The bundle is not signed or notarized: see README.md for how to open it the first time.
set -euo pipefail

version="${1:?usage: build_mac.sh <version>}"
if ! [[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "The version must look like 1.2.3" >&2
    exit 1
fi

cd "$(dirname "$0")"
python="${PYTHON:-python3}"

# The brand's tool icon; PyInstaller turns the PNG into an .icns with Pillow.
icon="assets/app-1024.png"

# The program reads its version from this file (About window, update check).
mkdir -p build
printf '%s' "$version" > build/version.txt

"$python" -m PyInstaller --noconfirm --clean --windowed --name "VPN Launcher" --icon "$icon" \
    --osx-bundle-identifier co.mancubus.vpnlauncher --collect-data sv_ttk \
    --add-data "$PWD/build/version.txt:." --add-data "$PWD/assets/mancubus-lab.png:." \
    --add-data "$PWD/assets/app.ico:." --add-data "$PWD/assets/icons:icons" \
    --distpath dist --workpath build --specpath build src/vpn_launcher.py

dmg="dist/VpnLauncher-$version-$(uname -m).dmg"
hdiutil create -volname "VPN Launcher" -srcfolder "dist/VPN Launcher.app" -ov -format UDZO "$dmg"
echo "Built: $dmg"
