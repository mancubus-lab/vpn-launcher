# VPN Launcher: agent notes

Tray / menu-bar app (Python) that runs several OpenVPN connections with one-time codes, optionally
taking credentials from the 1Password CLI. Windows is in daily use; macOS is built but less tested.
See `README.md` for features, security notes and the file map.

## Commands

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\pythonw.exe src\vpn_launcher.py   # run the app
.\build.ps1 -Version 1.0.0                      # PyInstaller folder + Inno Setup installer
```

`src/vpn_launcher.py` runs assertion-based self-checks of its pure functions at import time; a failed check
stops the start. Add a check there when you add pure logic. `sandbox.wsb` is a Windows Sandbox config for
trying the installer in isolation.

## Rules

- Never print, log or persist passwords, one-time codes or 1Password values. Credentials fetched from
  1Password stay in memory only.
- Keep every connection on `--script-security 1`; profile scripts (`up`, `down`, ...) stay blocked.
- Nothing runs elevated on Windows: if the OpenVPN service refuses, report and stop.
- `src/vpn_launcher.py` is shared by Windows and macOS: keep platform code in `vpn_connect_op.ps1`
  (Windows) and `vpn_mac.py` (macOS).
- Do not connect to a real VPN or read 1Password items unless explicitly asked.

## Releasing

Releases are built by `.github/workflows/release.yml` when a `vX.Y.Z` tag is pushed. Do not tag or push
unless explicitly asked.

## Related repositories

- App and tray icons in `assets/` come from `mancubus_lab_brand_assets` (`tool-vpn-launcher*.ico`).
- The product page lives in `mancubus_lab_site/vpn-launcher/`.
