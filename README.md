# VPN Launcher

A small Windows tray application that connects OpenVPN profiles with one click, including
servers that ask for a one-time code. Several connections can be up at the same time; each
one is connected and disconnected on its own.

## Features

- One card per connection: state, VPN address, uptime, live traffic speed and a two-minute graph.
- Three ways to authenticate, chosen per profile:
  - **1Password** – the `.ovpn` profile, the user name, the password and the one-time code are
    read with the 1Password CLI at connect time; nothing is stored by the launcher.
  - **Profile file, typed password** – an imported `.ovpn` and a password typed on every connect.
  - **Profile file, saved one-time code key** – the launcher generates the TOTP code itself.
- No elevation prompt on connect: OpenVPN is started through its interactive service.
- Tray icon with a menu, notifications, autostart, light / dark / automatic theme.
- A journal in the window, also written to `%TEMP%\vpn_connect_op\launcher.log` (about 2 MB at most).

## Requirements

- Windows 10 or 11, x64.
- [OpenVPN Community](https://openvpn.net/community-downloads/) client. The installer adds it through `winget`
  when it is missing.
- [1Password CLI](https://developer.1password.com/docs/cli/) (`op`) – only for profiles that use 1Password.

## Install

Run `VpnLauncherSetup-<version>.exe` from a release build. Setup asks for administrator rights once; connecting
afterwards never does.

The installer is not code-signed, so Windows SmartScreen warns about an unknown publisher.

## Where things are stored

| What | Where |
|---|---|
| Profiles and settings | `%APPDATA%\VpnLauncher\` |
| Imported `.ovpn` files | `%APPDATA%\VpnLauncher\vpn_launcher_profiles\` |
| Per-connection logs and runtime files | `%TEMP%\vpn_connect_op\<name>\` |

Security notes:

- An imported `.ovpn` is kept on disk as it is, including any private key inside it.
- A saved one-time code key is encrypted with Windows DPAPI: only the same Windows user on the same
  machine can read it. Keeping the key next to the profile means the code is no longer a second factor
  against someone who controls that Windows account. Prefer the 1Password mode where that matters.
- Passwords typed at connect time are handed to OpenVPN and never stored.

## Limits

- A connection that authenticates with a one-time code cannot reconnect by itself after a network drop:
  the code is single-use. Connect it again from the window.
- Two profiles that point at the same server with the same account cannot be up together.
- An imported profile must carry its certificates inline; files it references are not copied.
- Users without administrator rights must be members of the local `OpenVPN Administrators` group.
  Setup adds the account that approved the elevation; add other accounts by hand. Nothing is ever
  started elevated: when the OpenVPN service refuses a connection, the launcher reports it and stops.
- Scripts named in a profile (`up`, `down`, `route-up`, ...) are blocked: every connection runs with
  `--script-security 1`. Importing a profile that uses such directives asks for confirmation first.
- Only one instance runs at a time; starting it again brings the running window to the front.

## Development

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\pythonw.exe src\vpn_launcher.py
```

Layout:

| Path | Purpose |
|---|---|
| `src/vpn_launcher.py` | The window, the tray icon, profile editor, traffic graph |
| `src/vpn_connect_op.ps1` | Connects, reports and stops one named OpenVPN connection |
| `build.ps1` | Builds the PyInstaller folder and the installer |
| `installer.iss` | Inno Setup script |
| `assets/` | Application icon |

`vpn_launcher.py` checks its pure functions with assertions at import time; a failed check stops the start.

## Build

Needs [Inno Setup 6](https://jrsoftware.org/isinfo.php) for the installer.

```powershell
.\build.ps1 -Version 1.0.0
```

The result is `dist\VpnLauncherSetup-1.0.0.exe`.

## Release

Releases are built by GitHub Actions (`.github/workflows/release.yml`). Push a version tag:

```powershell
git tag v1.0.2
git push origin v1.0.2
```

The workflow builds the installer on a clean Windows runner and publishes a release with the
installer and its SHA-256 checksum. Running the workflow by hand from the Actions tab makes a
test build that is kept only as a workflow artifact.
