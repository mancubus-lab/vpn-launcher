<p align="center">
  <img src="https://github.com/{{REPO}}/raw/v{{VERSION}}/assets/icon.png" width="96" alt="VPN Launcher">
</p>

One-click OpenVPN connections with one-time codes, several at once, from the tray or the menu bar.

## 📦 Downloads

| | System | File | Notes |
|:-:|---|---|---|
| 🪟 | **Windows 10 / 11**, x64 | [VpnLauncherSetup-{{VERSION}}.exe](https://github.com/{{REPO}}/releases/download/v{{VERSION}}/VpnLauncherSetup-{{VERSION}}.exe) | Installer. Adds the OpenVPN client if it is missing. |
| 🍎 | **macOS**, Apple Silicon | [VpnLauncher-{{VERSION}}-arm64.dmg](https://github.com/{{REPO}}/releases/download/v{{VERSION}}/VpnLauncher-{{VERSION}}-arm64.dmg) | Disk image. New and not yet tried on a real VPN. |

🔐 Each download has a `.sha256` file next to it with its checksum.

## 🚀 Install

**🪟 Windows**

1. Run `VpnLauncherSetup-{{VERSION}}.exe`. The installer is not code-signed: in the SmartScreen warning choose **More info → Run anyway**.
2. Approve the administrator prompt. It appears once; connecting later never asks.
3. Start **VPN Launcher**, press **+ Add profile** and pick where the profile comes from.

**🍎 macOS**

1. Open the `.dmg` and drag **VPN Launcher** to Applications.
2. The app is not signed, so open it the first time with **right-click → Open**, or run
   `xattr -dr com.apple.quarantine "/Applications/VPN Launcher.app"`.
3. Install the prerequisites with [Homebrew](https://brew.sh): `brew install openvpn`
   (and `brew install 1password-cli` for profiles that use 1Password).
4. macOS asks for an administrator password on every connect: OpenVPN needs root there.

## ✨ What it does

- 🔌 One card per connection with state, address, uptime, live speed and a traffic graph
- 🔑 Credentials from **1Password**, a typed password, or a one-time code generated from a saved key
- 🧩 Several VPNs at the same time, each connected and disconnected on its own
- 🖥️ Tray icon (Windows) or menu bar item (macOS), notifications, autostart
- 🌗 Light, dark and automatic theme

📖 Full documentation: [README](https://github.com/{{REPO}}/blob/v{{VERSION}}/README.md)

---
