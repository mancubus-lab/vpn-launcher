"""Small window with a tray icon that connects OpenVPN profiles through vpn_connect_op.ps1.

A profile works in one of two ways:
  * 1Password: credentials, TOTP and the .ovpn profile are fetched inside that script,
    none of them passes through this program;
  * profile file: an imported .ovpn plus either a password typed on every connect or a
    one-time code generated from a saved key; both go to the script over stdin, the
    password is never stored and the key is stored encrypted with Windows DPAPI.
Traffic counters are read from the local OpenVPN management interface with the random
per-connection password the script saved.

Profiles, settings and imported .ovpn files live in %APPDATA%\\VpnLauncher and are edited in
the window. Every profile gets its own card and is connected and disconnected independently.

Run from a checkout without a console window:
    .venv\\Scripts\\pythonw.exe src\\vpn_launcher.py
"""
import base64
import ctypes
import hashlib
import hmac
import http.client
import json
import logging
import logging.handlers
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.request
import uuid
import webbrowser
from pathlib import Path
from tkinter import filedialog, font as tkfont, messagebox, ttk

import sv_ttk
from PIL import Image, ImageDraw, ImageTk

# One program for Windows and macOS. The window is shared; what talks to the system is chosen here:
# Win32 calls and the PowerShell connect script on Windows, the vpn_mac module on macOS.
WINDOWS = sys.platform == "win32"
if WINDOWS:
    import winreg

    import win32api
    import win32con
    import win32crypt
    import win32event
    import win32gui
    import win32process
else:
    import vpn_mac

FROZEN = getattr(sys, "frozen", False)  # True in the installed build made by PyInstaller
# Bundled files sit next to this file, or in PyInstaller's resource folder in the installed build.
RESOURCES = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
# Profiles, settings and imported .ovpn files live in the user profile, never next to the code:
# Program Files is read-only for the user, and a source checkout must not collect personal data.
DATA = Path(os.environ["APPDATA"]) / "VpnLauncher" if WINDOWS else vpn_mac.DATA
# How to start this program again (autostart entry, restart after a profile or theme change).
if FROZEN:
    SELF = [sys.executable]
elif WINDOWS:
    SELF = [str(Path(sys.executable).with_name("pythonw.exe")), str(Path(__file__).resolve())]
else:
    SELF = [sys.executable, str(Path(__file__).resolve())]
SCRIPT = str(RESOURCES / "vpn_connect_op.ps1")
PROFILES = DATA / "vpn_launcher_profiles.json"
PROFILE_DIR = DATA / "vpn_launcher_profiles"  # imported .ovpn files
GRAPH_POINTS = 60 # traffic samples kept for the graph: two minutes at one sample per 2 s
SETTINGS = DATA / "vpn_launcher_settings.json"
# One subfolder per connection with its log and runtime files.
WORK = Path(os.environ["TEMP"]) / "vpn_connect_op" if WINDOWS else vpn_mac.WORK
JOURNAL = WORK / "launcher.log"  # copy of the window's journal for later troubleshooting
JOURNAL_BYTES = 1_000_000  # per file; one older file (launcher.log.1) is kept, so at most twice this on disk
PWSH = shutil.which("pwsh") or "powershell"
NO_WINDOW = subprocess.CREATE_NO_WINDOW if WINDOWS else 0
REFRESH_MS = 2000
TRAY_MSG = 0x0400 + 20  # WM_USER + 20: our private message from the tray icon
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APP_ID = "VpnLauncher"
UPLOAD = "#3b7dd8"  # the upload line of the traffic graph; a chart series colour, not part of the brand tokens


def _mix(color, other, amount):
    """The hex colour `amount` (0..1) of the way from color to other."""
    a, b = (tuple(int(c[i:i + 2], 16) for i in (1, 3, 5)) for c in (color, other))
    return "#" + "".join(f"{round(x + (y - x) * amount):02x}" for x, y in zip(a, b))


def _solid(color, under):
    """Tk has no transparency: an rgba() token becomes the opaque colour it shows over `under`."""
    found = re.fullmatch(r"rgba\((\d+),\s*(\d+),\s*(\d+),\s*([\d.]+)\)", color.strip())
    if not found:
        return color.lower()
    r, g, b = (int(found.group(i)) for i in (1, 2, 3))
    return _mix(under, f"#{r:02x}{g:02x}{b:02x}", float(found.group(4)))


mix = _mix
assert mix("#000000", "#ffffff", 0.5) == "#808080" and _solid("rgba(255, 255, 255, 0.5)", "#000000") == "#808080"

# All colours come from the Mancubus Lab brand tokens: assets/tokens.json, a copy of tokens/tokens.json from
# mancubus-lab-brand-assets. Change them there and copy the file here. Read straight from the bundle folder or
# assets/, because resource() is defined further down.
_TOKENS_FILE = next(p for p in (RESOURCES / "tokens.json", RESOURCES.parent / "assets" / "tokens.json") if p.exists())
TOKENS = json.loads(_TOKENS_FILE.read_text(encoding="utf-8"))
_brand, _dark = TOKENS["brand"], TOKENS["themes"]["dark"]
STATE_TOKENS = {"off": "ink-soft", "connecting": "warning", "on": "success"}  # the token of each connection state
# The coloured padlock squares: the same in both themes, matching the tray, where connecting is orange.
COLORS = {"off": _dark["ink-soft"], "connecting": _brand["orange"], "on": _dark["success"]}
# Colours of the hand-drawn parts (cards, graph, log) per theme; "fill" is the tint under the download line.
PALETTES = {
    name: {
        "card": theme["panel"],
        "border": _solid(theme["line"], theme["panel"]),
        "muted": theme["ink-muted"],
        "fill": _mix(theme["panel"], theme["success"], 0.22),
        "text": theme["ink"],
        "log": theme["ink-muted"],
        "danger": theme["danger"],
        # State colours as text on the cards, per theme, so they keep their contrast on light panels too.
        **{state: theme[token] for state, token in STATE_TOKENS.items()},
    }
    for name, theme in TOKENS["themes"].items()
}
# The header, the About window and their pill buttons, per theme: charcoal in dark, white in light.
BRANDS = {
    name: {"bg": theme["panel"] if name == "light" else theme["bg"], "ink": theme["ink"], "muted": theme["ink-muted"],
           "accent": theme["accent"], "on-accent": theme["on-accent"], "link": theme["accent-text"],
           "panel": theme["panel-raised"], "on": theme["success"]}
    for name, theme in TOKENS["themes"].items()
}
BRAND = dict(BRANDS["dark"])  # main() switches it to the theme in use before anything is drawn
WEBSITE = "https://mancubus-lab.com/"
REPOSITORY = "mancubus-lab/vpn-launcher"  # releases of this repository are the update channel
GITHUB_RELEASE = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
GITHUB_DOWNLOADS = f"https://github.com/{REPOSITORY}/releases/download/"
# The site serves copies of the release files from Cloudflare, because GitHub's file servers can be very slow
# on some networks. Only the bytes come from there: which version to install and its checksum always come
# from GitHub, so the site alone cannot hand out a different installer. GitHub is the fallback download.
MIRROR_DOWNLOADS = "https://mancubus-lab.com/vpn-launcher/download/"
INSTALLER_NAME = re.compile(r"[\w.-]+\.(exe|dmg)")  # also keeps the saved file inside the update folder
UPDATE_CHECK_MS = 6 * 60 * 60 * 1000
NIIF_USER_LARGE = 0x4 | 0x20  # NIIF_USER | NIIF_LARGE_ICON: the notification shows our icon, not the blue "i"
# Setting key, checkbox label, default value.
CHECKBOXES = [
    ("autostart", "Start with Windows" if WINDOWS else "Start at login", False),
    ("start_minimized", "Start minimized to tray" if WINDOWS else "Start hidden in the menu bar", False),
    ("close_to_tray", "Close button minimizes to tray" if WINDOWS else "Close button hides to the menu bar", True),
    ("notify", "Show connection notifications", True),
    ("show_log", "Show log", False),
    ("check_updates", "Check for updates automatically", True),
]
THEMES = {"Auto": "auto", "Light": "light", "Dark": "dark"}  # label in the settings -> stored value


def resource(name):
    """A bundled file: next to the code in a build, in assets/ when running from a checkout."""
    bundled = RESOURCES / name
    return bundled if bundled.exists() else RESOURCES.parent / "assets" / name


def source_version():
    """Version of a copy run from a checkout: the latest release tag plus "-dev", or just "dev" without git."""
    try:
        tag = subprocess.run(["git", "-C", str(RESOURCES.parent), "describe", "--tags", "--abbrev=0"],
                             capture_output=True, text=True, timeout=5, creationflags=NO_WINDOW).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        tag = ""
    return f"{tag.lstrip('v')}-dev" if tag else "dev"


# The build writes the release version into version.txt. A checkout has none: it shows the tag it is based on
# with "-dev", which is not a release version, so such a copy never offers updates.
VERSION = resource("version.txt").read_text().strip() if resource("version.txt").exists() else source_version()


def parse_version(text):
    """(1, 2, 3) for "1.2.3" or "v1.2.3"; None for anything else, including "dev"."""
    found = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", text.strip())
    return tuple(int(part) for part in found.groups()) if found else None


def find_update(release, current, suffix):
    """(version, file name, download URLs, checksum) when a GitHub release is newer than the running version.

    suffix picks the download for this system. The checksum is the SHA-256 that GitHub computed for the file
    (the asset's "digest"), or for older releases the URL of the .sha256 file published next to it on GitHub.
    The URLs are tried in order: the mirror on the site, then GitHub.
    """
    latest, running = parse_version(str(release.get("tag_name", ""))), parse_version(current)
    if not latest or not running or latest <= running:
        return None
    assets = {asset.get("name", ""): asset for asset in release.get("assets", [])
              if str(asset.get("browser_download_url", "")).startswith(GITHUB_DOWNLOADS)}
    name = next((name for name in assets if name.endswith(suffix) and INSTALLER_NAME.fullmatch(name)), None)
    if not name:
        return None
    digest = str(assets[name].get("digest") or "")
    if re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        checksum = digest.split(":")[1]
    elif name + ".sha256" in assets:
        checksum = assets[name + ".sha256"]["browser_download_url"]
    else:
        return None
    version = ".".join(str(part) for part in latest)
    return version, name, [f"{MIRROR_DOWNLOADS}v{version}/{name}", assets[name]["browser_download_url"]], checksum


def _sample(names, digest=None, base=GITHUB_DOWNLOADS):
    return {"tag_name": "v1.2.0", "assets": [
        {"name": name, "browser_download_url": f"{base}v1.2.0/{name}", **({"digest": digest} if digest else {})}
        for name in names]}


assert parse_version("v1.10.3") == (1, 10, 3) and parse_version("dev") is None and parse_version("1.2.0-dev") is None
_found = find_update(_sample(["Setup-1.2.0.exe", "Setup-1.2.0.exe.sha256"]), "1.1.9", ".exe")
assert _found[:2] == ("1.2.0", "Setup-1.2.0.exe") and _found[2][0] == f"{MIRROR_DOWNLOADS}v1.2.0/Setup-1.2.0.exe"
assert _found[3] == f"{GITHUB_DOWNLOADS}v1.2.0/Setup-1.2.0.exe.sha256"
assert find_update(_sample(["Setup-1.2.0.exe"], "sha256:" + "a" * 64), "1.1.9", ".exe")[3] == "a" * 64
assert find_update(_sample(["Setup-1.2.0.exe"]), "1.1.9", ".exe") is None  # no checksum at all
assert find_update(_sample(["Setup-1.2.0.exe", "Setup-1.2.0.exe.sha256"]), "1.2.0", ".exe") is None
assert find_update(_sample(["Setup-1.2.0.exe", "Setup-1.2.0.exe.sha256"]), "dev", ".exe") is None
assert find_update(_sample(["Setup-1.2.0.exe", "Setup-1.2.0.exe.sha256"], base="https://evil/"), "1.0.0", ".exe") is None
assert find_update(_sample([r"..\x.exe", r"..\x.exe.sha256"]), "1.0.0", ".exe") is None  # unsafe file name


class DownloadCancelled(Exception):
    """Raised from the progress callback to stop a download the user cancelled."""


def fetch(url, limit=200_000_000, progress=None):
    """Downloads a URL over HTTPS and returns the bytes; progress(done, total) is called as the data arrives."""
    request = urllib.request.Request(url, headers={"User-Agent": "vpn-launcher", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(request, timeout=30) as reply:
        if not reply.geturl().startswith("https://"):  # a redirect may lead anywhere; plain HTTP is not accepted
            raise ValueError(f"Refused a redirect to {reply.geturl()}")
        total = int(reply.headers.get("Content-Length") or 0)
        parts, done = [], 0
        while done < limit:
            part = reply.read(min(65536, limit - done))
            if not part:
                break
            parts.append(part)
            done += len(part)
            if progress:
                progress(done, total)
        return b"".join(parts)


def progress_text(done, total):
    """ "42% (7.3 MB of 17.3 MB)", or just the amount when the server did not say how much is coming."""
    if total <= 0:
        return format_bytes(done)
    return f"{done * 100 // total}% ({format_bytes(done)} of {format_bytes(total)})"


# What a download can fail with: network errors, broken HTTP replies (not OSError), refused redirects.
NETWORK_ERRORS = (OSError, ValueError, http.client.HTTPException)


def check_update():
    """The newer release for this system as find_update() describes it, or None (also when offline)."""
    try:
        return find_update(json.loads(fetch(GITHUB_RELEASE)), VERSION, ".exe" if WINDOWS else "-arm64.dmg")
    except (*NETWORK_ERRORS, AttributeError, TypeError, KeyError):
        return None


def download_update(update, progress=None):
    """Downloads the installer of an update, checks it against the checksum from GitHub and returns the path.

    The mirror is tried first and GitHub second; a copy that does not match the checksum is never kept.
    """
    _, name, urls, checksum = update
    if checksum.startswith("https://"):  # an older release without a digest: its .sha256 file on GitHub
        checksum = fetch(checksum, 1000).decode("ascii", "replace").split()[0].lower()
    problems = []
    for url in urls:
        try:
            data = fetch(url, progress=progress)
        except NETWORK_ERRORS as error:
            problems.append(f"{url.split('/')[2]}: {error}")
            continue
        if hashlib.sha256(data).hexdigest() != checksum:
            problems.append(f"{url.split('/')[2]}: the file does not match its checksum")
            continue
        folder = WORK / "update"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        path.write_bytes(data)
        return path
    raise ValueError("; ".join(problems))


def icon_path(state):
    """The tray icon (.ico) of a connection state; the .png beside it is used for the macOS menu bar.

    The icons come from mancubus-lab-brand-assets (tool-vpn-launcher-tray-*): the lab's tool template with a
    padlock whose colour is the state. Copied into assets/icons; rebuild them there, not here.
    """
    return resource(f"icons/{state}.ico")


def lock_path(state):
    """The plain padlock in the state colour, for the cards and the window: .ico, with a .png beside it.

    Drawn on first use and cached in the data folder. The brand logo is kept for the tray and the header.
    """
    path = DATA / "vpn_launcher_icons" / f"lock_{state}.ico"
    if not path.with_suffix(".png").exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        color = COLORS[state]
        image = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((8, 8, 248, 248), radius=56, fill=color)
        draw.arc((82, 44, 174, 150), 180, 360, fill="white", width=24)  # shackle
        draw.rectangle((82, 96, 105, 124), fill="white")
        draw.rectangle((151, 96, 174, 124), fill="white")
        draw.rounded_rectangle((60, 116, 196, 212), radius=20, fill="white")  # body
        draw.ellipse((114, 140, 142, 168), fill=color)  # keyhole
        draw.rectangle((122, 160, 134, 190), fill=color)
        image.save(path, sizes=[(s, s) for s in (16, 20, 24, 32, 40, 48, 64)])
        image.save(path.with_suffix(".png"))
    return path


def load_icon(state, metric):
    size = win32api.GetSystemMetrics(metric)
    return win32gui.LoadImage(0, str(icon_path(state)), win32con.IMAGE_ICON, size, size, win32con.LR_LOADFROMFILE)


def register_app_id():
    """Makes Windows label notifications "VPN Launcher" instead of "Python"."""
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"Software\Classes\AppUserModelId\{APP_ID}") as key:
        winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, "VPN Launcher")
        winreg.SetValueEx(key, "IconUri", 0, winreg.REG_SZ, str(resource("app.ico")))
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)


def run_script(*args, stdin_text=""):
    """Runs the connect script; returns (exit code, output). Never raises: the caller's busy state depends on it."""
    if not WINDOWS:
        return vpn_mac.run(args, stdin_text, risky_directives)
    try:
        return _run_script(*args, stdin_text=stdin_text)
    except OSError as err:
        return 1, f"PowerShell could not be started: {err}"
    except subprocess.TimeoutExpired:
        return 1, f"The connect script did not finish within {SCRIPT_TIMEOUT // 60} minutes and was stopped."


# The script gives OpenVPN 90 s to connect; the rest is time to approve 1Password and the slack of a slow start.
SCRIPT_TIMEOUT = 300


def _run_script(*args, stdin_text):
    proc = subprocess.run(
        # Bypass: a fresh Windows blocks every script by default; this only covers our own bundled script.
        [PWSH, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", SCRIPT, *args],
        input=stdin_text, capture_output=True, text=True, encoding="utf-8", errors="replace",
        creationflags=NO_WINDOW, timeout=SCRIPT_TIMEOUT,
    )
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def format_uptime(seconds):
    minutes = int(seconds // 60)
    if minutes < 1:
        return "under a minute"
    return f"{minutes} min" if minutes < 60 else f"{minutes // 60} h {minutes % 60:02d} min"


def parse_status(process_running, log_text, uptime):
    """Returns (state, text) where state is one of the COLORS keys."""
    if not process_running:
        return "off", "Not connected"
    # Only what was logged after the last restart counts: an earlier "Completed" says nothing about the
    # tunnel now, and showing "Connected" while OpenVPN is reconnecting would hide that traffic bypasses it.
    restarted = "process restarting" in log_text
    if "Initialization Sequence Completed" not in log_text.rsplit("process restarting", 1)[-1]:
        return "connecting", "Reconnecting..." if restarted else "Connecting..."
    ips = re.findall(r"ifconfig (\d+\.\d+\.\d+\.\d+)", log_text)
    return "on", "  ·  ".join(["Connected", *ips[-1:], format_uptime(uptime)])


def overall_state(states):
    """One colour for the tray and the window icon: any connecting, else any connected, else off."""
    return next((s for s in ("connecting", "on") if s in states), "off")


assert parse_status(False, "", 0) == ("off", "Not connected")
assert parse_status(True, "", 0)[0] == "connecting"
assert parse_status(True, "ifconfig 10.8.0.6 255.255.255.0\nInitialization Sequence Completed", 3900) == (
    "on", "Connected  ·  10.8.0.6  ·  1 h 05 min")
assert parse_status(True, "Initialization Sequence Completed\nSIGUSR1[soft,ping-restart] received, process restarting",
                    0) == ("connecting", "Reconnecting...")
assert parse_status(True, "process restarting\nInitialization Sequence Completed", 0)[0] == "on"
assert format_uptime(59) == "under a minute" and format_uptime(600) == "10 min"
assert overall_state(["off", "on"]) == "on" and overall_state(["on", "connecting"]) == "connecting"
assert overall_state([None, "off"]) == "off"


def format_bytes(count):
    for unit in ("B", "KB", "MB"):
        if count < 1024:
            return f"{count:.0f} {unit}" if unit == "B" else f"{count:.1f} {unit}"
        count /= 1024
    return f"{count:.2f} GB"


def parse_traffic(line):
    """(bytes received, bytes sent) from a ">BYTECOUNT:in,out" line of the management interface."""
    found = re.match(r">BYTECOUNT:(\d+),(\d+)", line)
    return (int(found.group(1)), int(found.group(2))) if found else None


def traffic_rate(previous, current, seconds):
    """(bytes per second down, up) between two counter samples, or None without a previous sample."""
    if not previous or seconds <= 0:
        return None
    return tuple(max(now - before, 0) / seconds for before, now in zip(previous, current))


def traffic_cells(rate, totals):
    """Texts of the two-row traffic table: arrow, current speed (when known), "total", session total."""
    return [text for index, arrow in enumerate("↓↑")
            for text in (arrow, f"{format_bytes(rate[index])}/s" if rate else "", "total", format_bytes(totals[index]))]


def graph_points(values, width, height, peak):
    """Flat x, y list for a canvas line: oldest value on the left, the peak at the top."""
    step = width / (GRAPH_POINTS - 1)
    return [number for index, value in enumerate(values)
            for number in (index * step, height - 2 - value / peak * (height - 4))]


def totp(key, moment=None):
    """Six-digit time-based one-time code (RFC 6238, SHA-1, 30 s) for a base32 key."""
    padded = re.sub(r"[\s-]", "", key).upper()
    secret = base64.b32decode(padded + "=" * (-len(padded) % 8))
    counter = int((time.time() if moment is None else moment) // 30).to_bytes(8, "big")
    digest = hmac.new(secret, counter, hashlib.sha1).digest()
    start = digest[-1] & 0x0F
    return f"{(int.from_bytes(digest[start:start + 4], 'big') & 0x7FFFFFFF) % 1_000_000:06d}"


# RFC 6238 test vector: ASCII key "12345678901234567890" at T = 59 s gives 94287082, the last six digits here.
assert totp("GEZD GNBV GY3T QOJQ gezdgnbvgy3tqojq", 59) == "287082"


def protect(text):
    """Encrypts a secret with Windows DPAPI: only this Windows user on this machine can read it back."""
    if not WINDOWS:
        return vpn_mac.protect(text)  # the login Keychain; the returned handle is what gets saved
    return base64.b64encode(win32crypt.CryptProtectData(text.encode(), "VPN Launcher", None, None, None, 0)).decode()


def unprotect(blob):
    if not WINDOWS:
        return vpn_mac.unprotect(blob)
    return win32crypt.CryptUnprotectData(base64.b64decode(blob), None, None, None, 0)[1].decode()


if WINDOWS:  # not on macOS: a check at every start would leave an entry in the user's Keychain
    assert unprotect(protect("round trip")) == "round trip"


# A profile has three identities:
#   "id"   - its number, shown on the card (#3); assigned once, never reused while the profile exists;
#   "key"  - what the connect script and the work folder are called: str(id) for new profiles, the former
#            name for profiles made before numbers existed (their running VPNs keep their folders);
#   "name" - only a label for the user: any text, may repeat, can change at any time.
KEY_PATTERN = r"[\w-]+"  # the connect script checks the same pattern
LABEL_LENGTH = 60


def check_label(label):
    """The label as it is stored, or ValueError."""
    label = " ".join(label.split())
    if not label:
        raise ValueError("The name is empty")
    if len(label) > LABEL_LENGTH:
        raise ValueError(f"The name is longer than {LABEL_LENGTH} characters")
    return label


def check_profile(profile):
    """Raises ValueError when a profile entry cannot be used; id and key are checked when present."""
    name = check_label(profile.get("name", ""))
    if "key" in profile and not re.fullmatch(KEY_PATTERN, str(profile["key"])):
        raise ValueError(f"{name}: the internal key {profile['key']!r} is not valid")
    if "id" in profile and not (isinstance(profile["id"], int) and profile["id"] > 0):
        raise ValueError(f"{name}: the number {profile['id']!r} is not valid")
    keys = ("profile_path",) if profile.get("source") == "file" else ("vault", "item", "profile_file")
    for key in keys:
        if not profile.get(key):
            raise ValueError(f"{name}: {key.replace('_', ' ')} is empty")


def number_profiles(profiles):
    """Gives every profile without an id the next free number; True when something was added.

    A profile from before numbers keeps its name as the key, so its work folder (and a VPN running from
    it) stays the same; a new one is keyed by its number.
    """
    changed = False
    for profile in profiles:
        if "id" not in profile:
            profile["id"] = max((p.get("id", 0) for p in profiles), default=0) + 1
            changed = True
        if "key" not in profile:
            profile["key"] = profile["name"] if re.fullmatch(KEY_PATTERN, profile.get("name", "")) else str(profile["id"])
            changed = True
    return changed


def label_of(profile):
    return f"#{profile['id']} {profile['name']}"


_old = [{"name": "Prod"}, {"name": "Dev 1 pass"}, {"name": "x", "id": 7, "key": "7"}]
assert number_profiles(_old) and [(p["id"], p["key"]) for p in _old] == [(8, "Prod"), (9, "9"), (7, "7")]
assert not number_profiles(_old) and label_of(_old[0]) == "#8 Prod"
assert check_label("  Dev   1 pass ") == "Dev 1 pass"


# Profile directives that make OpenVPN run a program, load code or write a file of the profile's choosing.
RISKY_DIRECTIVES = {
    "up", "down", "route-up", "route-pre-down", "ipchange", "tls-verify", "auth-user-pass-verify",
    "client-connect", "client-disconnect", "learn-address", "tls-crypt-v2-verify", "plugin",
    "pkcs11-providers", "providers", "management", "status", "writepid", "log", "log-append",
    # Read another file the check cannot see, or write or move files of the profile's choosing.
    "config", "replay-persist", "tls-export-cert", "cd", "chroot", "tmp-dir", "daemon", "askpass",
}


def risky_directives(profile_text):
    """Sorted names of the RISKY_DIRECTIVES an .ovpn uses; inline <ca>...</ca> style blocks are skipped.

    Written the way OpenVPN reads a line: the name may be quoted or start with "--", and "setenv opt X"
    is the directive X itself (only that OpenVPN does not stop when it does not know X).
    """
    found, inside = set(), False
    for line in profile_text.splitlines():
        words = [word.strip("\"'").lower() for word in line.split()]
        if not words or not words[0]:
            continue
        if words[0].startswith("</"):
            inside = False
        elif words[0].startswith("<"):
            inside = True
        elif not inside:
            if words[0].lstrip("-") == "setenv" and len(words) > 2 and words[1] == "opt":
                words = words[2:]
            if words[0].lstrip("-") in RISKY_DIRECTIVES:
                found.add(words[0].lstrip("-"))
    return sorted(found)


assert risky_directives("client\nUP 'cmd /c x'\n<ca>\nplugin not-a-directive\n</ca>\n  plugin x.dll\n# up\n") == [
    "plugin", "up"]
assert risky_directives("client\nremote vpn.example 1194\nscript-security 2\nsetenv FOO bar\n") == []
assert risky_directives('setenv opt plugin x.so\n"tls-verify" x\nconfig b.conf\nreplay-persist C:/x\n--cd /tmp\n') == [
    "cd", "config", "plugin", "replay-persist", "tls-verify"]


def load_profiles():
    """Profiles from the JSON file; no file means a fresh install with nothing configured yet."""
    if not PROFILES.exists():
        return []
    profiles = json.loads(PROFILES.read_text(encoding="utf-8"))
    if number_profiles(profiles):
        save_json(PROFILES, profiles)
    for profile in profiles:
        check_profile(profile)
    # Compared without case: the key is a folder name, and Windows folders ignore case.
    if len({str(p["key"]).casefold() for p in profiles}) != len(profiles) or \
            len({p["id"] for p in profiles}) != len(profiles):
        raise ValueError("profile numbers must be unique")
    return profiles


assert format_bytes(512) == "512 B" and format_bytes(1536) == "1.5 KB" and format_bytes(3 * 1024 ** 3) == "3.00 GB"
assert progress_text(512 * 1024, 2 * 1024 * 1024) == "25% (512.0 KB of 2.0 MB)" and progress_text(2048, 0) == "2.0 KB"
assert parse_traffic(">BYTECOUNT:2048,10\n") == (2048, 10)
assert parse_traffic("ENTER PASSWORD:SUCCESS: password is correct") is None
assert traffic_rate((0, 0), (2048, 1024), 2) == (1024, 512) and traffic_rate(None, (1, 1), 2) is None
assert traffic_cells((1024, 512), (2048, 1024)) == ["↓", "1.0 KB/s", "total", "2.0 KB", "↑", "512 B/s", "total", "1.0 KB"]
assert traffic_cells(None, (2048, 1024))[:4] == ["↓", "", "total", "2.0 KB"]
assert graph_points([0, 10], 59, 24, 10) == [0, 22, 1, 2]
check_profile({"name": "Lab", "source": "file", "profile_path": "x.ovpn"})
check_profile({"name": "Dev 1 pass / прод", "source": "file", "profile_path": "x.ovpn", "id": 3, "key": "3"})
for bad in ({"name": "  ", "vault": "v", "item": "i", "profile_file": "f"}, {"name": "Lab", "source": "file"},
            {"name": "Lab", "source": "file", "profile_path": "x", "key": "a/b"},
            {"name": "Lab", "source": "file", "profile_path": "x", "id": 0}):
    try:
        check_profile(bad)
        raise AssertionError(bad)
    except ValueError:
        pass


PASSWORD_PROMPT = ">PASSWORD:Need 'Auth'"


def management_argument(text):
    """A management command argument: in double quotes, with backslashes and quotes escaped."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


assert management_argument('a"b\\') == '"a\\"b\\\\"'


def watch_traffic(name, links, samples, on_password):
    """Thread body: holds one management link open and stores the counters OpenVPN pushes every 2 s.

    One long-lived link instead of polling, because OpenVPN logs every management connection and command.
    The link is the only one OpenVPN accepts, so it must be closed before the script stops the connection.
    When OpenVPN asks for the credentials again (a key renegotiation or a restart), on_password(name, link)
    answers; it returns True when it gave the link away, and the next refresh opens a new one.
    """
    try:
        port = int((WORK / name / "port.txt").read_text())
        password = (WORK / name / "mgmt.txt").read_text().split()[0]
        with socket.create_connection(("127.0.0.1", port), timeout=5) as link:
            links[name] = link
            link.settimeout(None)
            link.sendall(f"{password}\nbytecount 2\n".encode())
            for line in link.makefile("r", errors="replace"):
                if line.startswith(PASSWORD_PROMPT) and on_password(name, link):
                    break
                traffic = parse_traffic(line)
                if traffic:
                    samples[name] = (time.monotonic(), *traffic)
    except (OSError, ValueError, IndexError):
        pass  # not reachable or closed: the caller starts a new watcher on the next refresh
    finally:
        links.pop(name, None)
        samples.pop(name, None)


def process_start(pid):
    """When a process started, in seconds since the epoch; None when that cannot be found out."""
    if not WINDOWS:
        return vpn_mac.process_start(pid)
    try:
        handle = win32api.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        try:
            return win32process.GetProcessTimes(handle)["CreationTime"].timestamp()
        finally:
            win32api.CloseHandle(handle)
    except (win32api.error, ValueError, OverflowError):
        return None


def started_with(started, written):
    """True when a process that started at `started` can be the one a PID file written at `written` names.

    The file is written a moment after the start (up to about a minute on a slow start through the service).
    An unknown start time counts as a match: better a stale card than a live VPN shown as off.
    """
    return started is None or written - 120 <= started <= written + 5


assert started_with(1000, 1003) and started_with(None, 1000)
assert not started_with(5000, 1000) and not started_with(100, 1000)


def vpn_status(names):
    """Returns {name: (state, text, log_text)}; a connection is alive while the PID saved for it is an openvpn process."""
    if WINDOWS:
        tasks = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq openvpn.exe", "/NH", "/FO", "CSV"],
            capture_output=True, text=True, creationflags=NO_WINDOW,
        ).stdout
        pids = set(re.findall(r'^"openvpn\.exe","(\d+)"', tasks, re.MULTILINE | re.IGNORECASE))
    else:
        pids = vpn_mac.openvpn_pids()
    result = {}
    for name in names:
        pid_file, log_file = WORK / name / "pid.txt", WORK / name / "openvpn.log"
        try:
            pid, written = pid_file.read_text().strip(), pid_file.stat().st_mtime
            # The file is written when OpenVPN starts. A process with that number that started at another
            # time is someone else's, which got the number after ours had ended (after a reboot, say).
            alive = pid in pids and started_with(process_start(pid), written)
            uptime = time.time() - written
        except OSError:
            alive, uptime = False, 0
        log_text = log_file.read_text(errors="replace") if log_file.exists() else ""
        result[name] = (*parse_status(alive, log_text, uptime), log_text)
    return result


def save_json(path, data):
    """Writes to a side file and swaps it in: a crash mid-write must not leave a truncated file behind."""
    draft = path.with_suffix(".tmp")
    draft.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(draft, path)


def load_settings():
    settings = {key: default for key, _, default in CHECKBOXES}
    settings["theme"] = "auto"
    try:
        settings.update(json.loads(SETTINGS.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass  # first run or a damaged file: fall back to the defaults
    return settings


def resolve_theme(choice):
    """"light" or "dark"; "auto" (and anything unknown) follows the system's app mode."""
    if choice in ("light", "dark"):
        return choice
    if not WINDOWS:
        return vpn_mac.system_theme()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            return "light" if winreg.QueryValueEx(key, "AppsUseLightTheme")[0] else "dark"
    except OSError:
        return "light"


assert resolve_theme("dark") == "dark" and resolve_theme("auto") in ("light", "dark")


def on_screen(geometry):
    """True when the top-left corner of a saved "WxH+X+Y" lies on the current desktop (all monitors)."""
    found = re.fullmatch(r"\d+x\d+\+(-?\d+)\+(-?\d+)", geometry or "")
    if not found:
        return False
    x, y = int(found.group(1)), int(found.group(2))
    if not WINDOWS:
        # shortcut: a generous range instead of the real monitor layout, macOS moves a stray window back itself.
        return -5000 <= x < 10000 and 0 <= y < 6000
    left, top, width, height = (ctypes.windll.user32.GetSystemMetrics(index) for index in (76, 77, 78, 79))
    return left <= x < left + width - 100 and top <= y < top + height - 100


assert not on_screen("") and not on_screen("800x600+999999+0") and not on_screen("garbage")


def set_autostart(enabled):
    if not WINDOWS:
        vpn_mac.set_autostart(enabled, SELF)
        return
    command = subprocess.list2cmdline(SELF)
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, "VpnLauncher", 0, winreg.REG_SZ, command)
        else:
            try:
                winreg.DeleteValue(key, "VpnLauncher")
            except FileNotFoundError:
                pass


def confirm(root, text):
    """Own Yes/No dialog: the system one labels its buttons in the Windows language and opens at the screen centre."""
    answer = []
    box = new_dialog(root)
    ttk.Label(box, text=text, padding=(40, 24, 40, 16)).pack()
    row = ttk.Frame(box, padding=(16, 0, 16, 16))
    row.pack()

    def close(value):
        answer.append(value)
        box.destroy()

    yes = ttk.Button(row, text="Yes", style="Accent.TButton", command=lambda: close(True))
    yes.pack(side="left", padx=4)
    ttk.Button(row, text="No", command=lambda: close(False)).pack(side="left", padx=4)
    box.bind("<Return>", lambda event: close(True))
    box.bind("<Escape>", lambda event: close(False))
    show_modal(root, box, yes)
    return answer == [True]


def show_modal(root, box, focus):
    """Centres a dialog over the main window, makes it modal and waits until it is closed."""
    box.update_idletasks()
    width, height = box.winfo_reqwidth(), box.winfo_reqheight()
    if root.winfo_viewable():
        box.transient(root)  # stays above the main window
        # winfo_x/y are the outer frame, the same thing the geometry offset positions.
        x = root.winfo_x() + (root.winfo_width() - width) // 2
        y = root.winfo_y() + (root.winfo_height() - height) // 2
    else:  # asked from the tray menu while the main window is hidden
        x, y = (box.winfo_screenwidth() - width) // 2, (box.winfo_screenheight() - height) // 2
    # The title bar can be coloured only once Windows has created the real window, which happens when it
    # is first shown. So it is first shown far off the screen, coloured there, and then moved into place.
    box.geometry("+-20000+-20000")
    box.deiconify()
    match_title_bar(box)
    box.geometry(f"+{x}+{y}")
    box.lift()
    box.grab_set()
    focus.focus_force()
    root.wait_window(box)


def match_title_bar(window):
    """Makes the Windows title bar dark when the dark theme is on; it does not follow the ttk theme."""
    if not WINDOWS:
        return  # macOS draws the title bar in the system appearance
    window.update_idletasks()
    dark = ctypes.c_int(sv_ttk.get_theme() == "dark")
    ctypes.windll.dwmapi.DwmSetWindowAttribute(  # 20 = DWMWA_USE_IMMERSIVE_DARK_MODE
        ctypes.windll.user32.GetParent(window.winfo_id()), 20, ctypes.byref(dark), ctypes.sizeof(dark))


def set_window_icon(window, state):
    """Title bar and taskbar icon: the brand icon of the state, the same as in the tray.

    macOS windows have none: the Dock icon comes from the app bundle.
    """
    if WINDOWS:
        window.iconbitmap(icon_path(state))


def display_scale(root):
    """Screen pixels per design pixel. Tk works in real pixels on Windows only; macOS scales windows itself."""
    return root.winfo_fpixels("1i") / 96 if WINDOWS else 1.0


def new_dialog(root):
    box = tk.Toplevel(root)
    box.withdraw()  # hidden until show_modal has placed it; otherwise it flashes at a default spot first
    box.title("VPN Launcher")
    box.configure(bg=ttk.Style().lookup("TFrame", "background"))  # a Toplevel does not follow the ttk theme
    box.resizable(False, False)
    set_window_icon(box, "on")
    return box


def field(parent, row, label, variable, **options):
    """A labelled entry on one grid row; returns the entry."""
    ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=3)
    entry = ttk.Entry(parent, textvariable=variable, width=36, **options)
    entry.grid(row=row, column=1, sticky="we", pady=3)
    return entry


def pill_button(parent, text, command, primary=False):
    """Rounded button for the branded areas (header, About), where the themed ttk buttons look out of place.

    The shape is a picture drawn with Pillow (Tk has no rounded widgets) with the text centred on it;
    primary=True gives the orange accent button, otherwise a quiet one in the raised panel colour.
    """
    scale = display_scale(parent)
    font = tkfont.Font(family="Segoe UI", size=10)
    width = font.measure(text) + round(30 * scale)
    height = font.metrics("linespace") + round(14 * scale)

    def shape(color):
        # Drawn four times larger and scaled down: that is what smooths the corners.
        large = Image.new("RGBA", (width * 4, height * 4), (0, 0, 0, 0))
        ImageDraw.Draw(large).rounded_rectangle((0, 0, width * 4 - 1, height * 4 - 1),
                                                radius=round(7 * scale) * 4, fill=color)
        return ImageTk.PhotoImage(large.resize((width, height), Image.LANCZOS))

    normal, hover = ((shape(BRAND["accent"]), shape(mix(BRAND["accent"], BRAND["ink"], 0.15))) if primary
                     else (shape(BRAND["panel"]), shape(mix(BRAND["panel"], BRAND["ink"], 0.1))))
    button = tk.Label(parent, image=normal, text=text, compound="center", font=font,
                      fg=BRAND["on-accent"] if primary else BRAND["ink"],
                      bg=BRAND["bg"], cursor="hand2", borderwidth=0, takefocus=True)
    button.shapes = normal, hover  # Tk does not keep the pictures alive by itself
    button.bind("<Enter>", lambda event: button.configure(image=hover))
    button.bind("<Leave>", lambda event: button.configure(image=normal))
    button.bind("<Button-1>", lambda event: command())
    button.bind("<Return>", lambda event: command())
    return button


def show_about(root, check_for_updates):
    """About window in the Mancubus Lab colours: logo, version, copyright and a link to the site."""
    box = new_dialog(root)
    box.configure(bg=BRAND["bg"])
    body = tk.Frame(box, bg=BRAND["bg"], padx=44, pady=30)
    body.pack()

    def text(content, color, font, **packing):
        label = tk.Label(body, text=content, bg=BRAND["bg"], fg=color, font=font)
        label.pack(**packing)
        return label

    try:
        side = round(104 * display_scale(root))
        logo = ImageTk.PhotoImage(Image.open(resource("mancubus-lab.png")).resize((side, side), Image.LANCZOS))
        picture = tk.Label(body, image=logo, bg=BRAND["bg"])
        picture.image = logo  # Tk does not keep the picture alive by itself
        picture.pack()
    except OSError:
        pass  # the logo file is missing: the text below says the same
    text("VPN Launcher", BRAND["ink"], ("Segoe UI Semibold", 17), pady=(16, 0))
    text(f"Version {VERSION}", BRAND["muted"], ("Segoe UI", 10))
    text("One-click OpenVPN connections with one-time codes.", BRAND["muted"], ("Segoe UI", 10), pady=(12, 14))
    link = text("mancubus-lab.com", BRAND["link"], ("Segoe UI", 11, "underline"))
    link.configure(cursor="hand2")
    link.bind("<Button-1>", lambda event: webbrowser.open(WEBSITE))
    text("© 2026 Mancubus Lab", BRAND["muted"], ("Segoe UI", 9), pady=(6, 18))

    def check():
        box.destroy()
        check_for_updates()

    row = tk.Frame(body, bg=BRAND["bg"])
    row.pack()
    pill_button(row, "Check for updates", check).pack(side="left", padx=5)
    close = pill_button(row, "Close", box.destroy, primary=True)
    close.pack(side="left", padx=5)
    box.bind("<Escape>", lambda event: box.destroy())
    show_modal(root, box, close)


def ask_credentials(root, name, username):
    """Asks for the user name and the password of a profile without 1Password; (user, password) or None."""
    answer = []
    box = new_dialog(root)
    body = ttk.Frame(box, padding=16)
    body.pack()
    ttk.Label(body, text=f"Connect {name}", font=("Segoe UI", 11, "bold")).grid(
        row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
    user, password = tk.StringVar(value=username), tk.StringVar()
    user_entry = field(body, 1, "User name", user)
    password_entry = field(body, 2, "Password", password, show="•")
    ttk.Label(body, text="Add the one-time code to the password if the server expects it.\n"
                         "The password is handed to OpenVPN and is not saved.",
              foreground="gray").grid(row=3, column=0, columnspan=2, sticky="w", pady=(4, 10))

    def accept():
        if user.get().strip():
            answer.append((user.get().strip(), password.get()))
            box.destroy()

    row = ttk.Frame(body)
    row.grid(row=4, column=0, columnspan=2)
    ttk.Button(row, text="Connect", style="Accent.TButton", command=accept).pack(side="left", padx=4)
    ttk.Button(row, text="Cancel", command=box.destroy).pack(side="left", padx=4)
    box.bind("<Return>", lambda event: accept())
    box.bind("<Escape>", lambda event: box.destroy())
    show_modal(root, box, password_entry if username else user_entry)
    return answer[0] if answer else None


def edit_profile(root, profile, taken):
    """Profile editor; returns ("save", profile), ("delete", None) or None when it was cancelled.

    A profile either takes everything from 1Password or uses an imported .ovpn file and asks for the password.
    """
    answer = []
    old = profile or {}
    box = new_dialog(root)
    body = ttk.Frame(box, padding=16)
    body.pack()
    defaults = {"name": "", "source": "op", "vault": "", "item": "", "profile_file": "",
                "otp_field": "one-time password", "otp_mode": "Auto", "profile_path": "", "username": ""}
    values = {key: tk.StringVar(value=old.get(key, default)) for key, default in defaults.items()}

    name_entry = field(body, 0, "Name", values["name"])
    ttk.Label(body, text="Source").grid(row=1, column=0, sticky="w", pady=3)
    radios = ttk.Frame(body)
    radios.grid(row=1, column=1, sticky="w")
    panels = {"op": ttk.Frame(body), "file": ttk.Frame(body)}

    def switch():
        for source, panel in panels.items():
            if source == values["source"].get():
                panel.grid(row=2, column=0, columnspan=2, sticky="we")
            else:
                panel.grid_remove()

    for source, label in (("op", "1Password"), ("file", "Profile file, ask for the password")):
        ttk.Radiobutton(radios, text=label, value=source, variable=values["source"],
                        command=switch).pack(side="left", padx=(0, 12))

    # Where to take things from in 1Password.
    field(panels["op"], 0, "Vault", values["vault"])
    field(panels["op"], 1, "Item (user name, password)", values["item"])
    field(panels["op"], 2, "Profile attachment (.ovpn)", values["profile_file"])
    field(panels["op"], 3, "One-time password field", values["otp_field"])
    ttk.Label(panels["op"], text="One-time code is sent").grid(row=4, column=0, sticky="w", padx=(0, 12), pady=3)
    ttk.Combobox(panels["op"], textvariable=values["otp_mode"], state="readonly", width=34,
                 values=("Auto", "Append", "Scrv1", "None")).grid(row=4, column=1, sticky="we", pady=3)

    def browse():
        chosen = filedialog.askopenfilename(parent=box, title="Import an OpenVPN profile",
                                            filetypes=[("OpenVPN profile", "*.ovpn *.conf"), ("All files", "*.*")])
        if chosen:
            values["profile_path"].set(chosen)

    # The program keeps its own copy of the profile, so the field shows that state instead of a path to type.
    shown = tk.StringVar()

    def describe(*_):
        path = Path(values["profile_path"].get() or "-")
        shown.set("" if path.name == "-" else "Imported profile  (own copy)" if path.parent == PROFILE_DIR
                  else f"{path.name}  (will be copied on Save)")

    values["profile_path"].trace_add("write", describe)
    describe()
    field(panels["file"], 0, "Profile (.ovpn)", shown, state="readonly")
    ttk.Button(panels["file"], text="Import...", command=browse).grid(row=0, column=2, padx=(6, 0))
    field(panels["file"], 1, "User name (optional)", values["username"])
    # With a key the program makes the one-time code itself and connects without asking for a password.
    new_key, drop_key = tk.StringVar(), tk.BooleanVar()
    field(panels["file"], 2, "One-time code key (optional)", new_key, show="•")
    if old.get("otp_key"):
        ttk.Label(panels["file"], text="A key is saved. Leave the field empty to keep it.",
                  foreground="gray").grid(row=3, column=1, sticky="w")
        ttk.Checkbutton(panels["file"], text="Remove the saved key", variable=drop_key).grid(
            row=4, column=1, sticky="w")
    else:
        ttk.Label(panels["file"], text="Base32 secret of the authenticator; stored encrypted for this Windows user." if WINDOWS
                  else "Base32 secret of the authenticator; stored in your macOS Keychain.",
                  foreground="gray").grid(row=3, column=1, columnspan=2, sticky="w")

    error = ttk.Label(body, text="", foreground=PALETTES[sv_ttk.get_theme()]["danger"], wraplength=420)
    error.grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))

    def save():
        source = values["source"].get()
        keys = ("profile_path", "username") if source == "file" else (
            "vault", "item", "profile_file", "otp_field", "otp_mode")
        new = {"name": values["name"].get().strip(), "source": source,
               **{key: values[key].get().strip() for key in keys}}
        try:
            new["name"] = check_label(new["name"])  # only a label: any text, it may repeat
            check_profile(new)
            if source == "file":
                if new_key.get().strip():
                    try:
                        totp(new_key.get())
                    except ValueError:
                        raise ValueError("The one-time code key is not a valid base32 secret") from None
                    new["otp_key"] = protect(new_key.get().strip())
                elif old.get("otp_key") and not drop_key.get():
                    new["otp_key"] = old["otp_key"]
                if new.get("otp_key") and not new["username"]:
                    raise ValueError("A user name is needed to connect with the one-time code key")
                chosen = Path(new["profile_path"])
                if not chosen.is_file():
                    raise ValueError("The profile file was not found")
                if chosen.parent != PROFILE_DIR:  # import: keep an own copy in the program's data folder
                    risky = risky_directives(chosen.read_text(encoding="utf-8", errors="replace"))
                    if risky:
                        agreed = confirm(box, "This profile asks OpenVPN to run programs or write files:\n"
                                              f"{', '.join(risky)}\n\n"
                                              "Scripts are blocked on connect, so it may not work as intended.\n"
                                              "Import it only if you trust where it came from. Import?")
                        box.grab_set()  # the question took the modal grab; give it back to the editor
                        if not agreed:
                            return
                    PROFILE_DIR.mkdir(exist_ok=True)
                    # A random name: one derived from the profile name could overwrite another profile's copy.
                    new["profile_path"] = str(PROFILE_DIR / f"{uuid.uuid4().hex}.ovpn")
                    shutil.copyfile(chosen, new["profile_path"])
        except (ValueError, OSError) as err:
            error.configure(text=str(err))
            return
        answer.append(("save", new))
        box.destroy()

    def delete():
        answer.append(("delete", None))
        box.destroy()

    row = ttk.Frame(body)
    row.grid(row=4, column=0, columnspan=2, pady=(12, 0))
    ttk.Button(row, text="Save", style="Accent.TButton", command=save).pack(side="left", padx=4)
    if profile:
        ttk.Button(row, text="Delete", command=delete).pack(side="left", padx=4)
    ttk.Button(row, text="Cancel", command=box.destroy).pack(side="left", padx=4)
    box.bind("<Escape>", lambda event: box.destroy())
    switch()
    show_modal(root, box, name_entry)
    return answer[0] if answer else None


def first_instance():
    """True when this process may run; otherwise it brings the running instance's window to the front.

    Two instances would fight over the single OpenVPN management link and overwrite each other's files.
    The mutex name is also the AppMutex of the installer, which uses it to see that the program is open.
    """
    if not WINDOWS:
        return vpn_mac.first_instance(wait="--show" in sys.argv)
    global _instance_mutex  # kept for the life of the process: Windows frees it when the process ends
    _instance_mutex = win32event.CreateMutex(None, True, "VpnLauncherSingleInstance")
    if win32api.GetLastError() != 183:  # ERROR_ALREADY_EXISTS
        return True
    # After a self-restart the previous instance is still on its way out: wait for it to let go.
    waited = win32event.WaitForSingleObject(_instance_mutex, 8000 if "--show" in sys.argv else 0)
    if waited in (win32event.WAIT_OBJECT_0, win32event.WAIT_ABANDONED):
        return True
    try:  # same message as a left click on the tray icon
        win32gui.PostMessage(win32gui.FindWindow("VpnLauncherTray", None), TRAY_MSG, 0, win32con.WM_LBUTTONUP)
    except win32gui.error:
        pass
    return False


def release_instance():
    """Gives up the single-instance mutex before the process ends, so that an installer started now sees none."""
    if WINDOWS and _instance_mutex:
        try:
            win32event.ReleaseMutex(_instance_mutex)
            win32api.CloseHandle(_instance_mutex)
        except win32api.error:
            pass  # not owned or already closed: nothing left to give up


_instance_mutex = None


def main():
    if WINDOWS:
        # Without this Windows bitmap-scales the window on a scaled display and it looks blurry.
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    if not first_instance():
        return
    DATA.mkdir(parents=True, exist_ok=True)
    if WINDOWS:
        register_app_id()
    root = tk.Tk()
    root.title(f"VPN Launcher {VERSION}")
    set_window_icon(root, "off")
    settings = load_settings()
    if settings["autostart"] and FROZEN:
        # Rewritten at every start, not only when the switch is turned on: the entry must point at the program
        # that runs now, and it can be missing although the setting is on (written from another place, or
        # removed by a cleanup tool). A copy run from source leaves the installed program's entry alone.
        try:
            set_autostart(True)
        except OSError:
            pass  # the switch in the settings still shows the wish; nothing else depends on the entry
    theme = resolve_theme(settings["theme"])
    ui = PALETTES[theme]
    BRAND.update(BRANDS[theme])
    sv_ttk.set_theme(theme)  # Windows 11 look for the ttk widgets
    root.configure(bg=ttk.Style().lookup("TFrame", "background"))
    match_title_bar(root)
    # The theme sizes its fonts in pixels, which stay tiny on a scaled display.
    for font_name in tkfont.names(root):
        if font_name.startswith("SunValley"):
            theme_font = tkfont.nametofont(font_name)
            theme_font.configure(size=round(theme_font.cget("size") * display_scale(root)))

    try:
        profiles = load_profiles()
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as err:
        messagebox.showerror("VPN Launcher", f"Cannot read {PROFILES}:\n{err}")
        return
    names = [p["key"] for p in profiles]  # the internal keys: work folders, script names, dictionary keys

    def label_for(key):
        """What the user sees for a connection: its number and its name."""
        return next((label_of(p) for p in profiles if p["key"] == key), key)

    # Per connection: vpn is a COLORS key (None until the first check), busy is the label of a running action.
    # traffic is the last sample: (time, bytes in, bytes out).
    # rates is the history for the graph: (bytes per second down, up).
    conns = {name: {"vpn": None, "busy": "", "log": "", "traffic": None, "rates": []} for name in names}
    app = {"overall": None, "update": None}
    links, samples = {}, {}  # per connection: the open management link and the newest (time, in, out)

    # Header in the Mancubus Lab colours of the theme: logo, name, a one-line summary, and the About and update buttons.
    header = tk.Frame(root, bg=BRAND["bg"], padx=18, pady=12)
    header.pack(fill="x")
    tk.Frame(root, bg=BRAND["accent"], height=max(round(3 * display_scale(root)), 2)).pack(fill="x")
    try:
        mark_side = round(40 * display_scale(root))
        mark = ImageTk.PhotoImage(Image.open(resource("mancubus-lab.png")).resize((mark_side, mark_side), Image.LANCZOS))
        tk.Label(header, image=mark, bg=BRAND["bg"]).pack(side="left", padx=(0, 12))
    except OSError:
        mark = None  # the logo file is missing: the header shows the text alone
    titles = tk.Frame(header, bg=BRAND["bg"])
    titles.pack(side="left")
    # The version is in the window title (and in About): repeating it here would only add noise.
    tk.Label(titles, text="Mancubus Lab", bg=BRAND["bg"], fg=BRAND["ink"], font=("Segoe UI Semibold", 14)).pack(anchor="w")
    summary = tk.Label(titles, text="Checking...", bg=BRAND["bg"], fg=BRAND["muted"], font=("Segoe UI", 9))
    summary.pack(anchor="w")
    pill_button(header, "About", lambda: about()).pack(side="right")
    # The orange update button is put here by look_for_update() when a newer release exists.

    frame = ttk.Frame(root, padding=16)
    frame.pack(fill="both", expand=True)
    # The same padlock as in the title bar, sized for the display scale.
    side = round(36 * display_scale(root))
    pictures = {name: ImageTk.PhotoImage(Image.open(lock_path(name).with_suffix(".png")).resize((side, side)))
                for name in COLORS}

    log_box = tk.Frame(frame, bg=ui["card"], highlightthickness=1, highlightbackground=ui["border"])
    log = tk.Text(log_box, height=12, width=60, state="disabled", font=("Cascadia Mono", 9), wrap="word",
                  bg=ui["card"], fg=ui["log"], insertbackground=ui["log"], relief="flat", borderwidth=0, padx=10, pady=8)
    log_bar = ttk.Scrollbar(log_box, command=log.yview)
    log.configure(yscrollcommand=log_bar.set)
    log_bar.pack(side="right", fill="y")
    log.pack(side="left", fill="both", expand=True)

    WORK.mkdir(parents=True, exist_ok=True)
    journal = logging.getLogger("vpn_launcher")
    journal.setLevel(logging.INFO)
    journal_file = logging.handlers.RotatingFileHandler(JOURNAL, maxBytes=JOURNAL_BYTES, backupCount=1,
                                                        encoding="utf-8")
    journal_file.setFormatter(logging.Formatter("%(asctime)s  %(message)s"))
    journal.addHandler(journal_file)
    journal.info("VPN Launcher started")
    # The installed build has no console: without this an error in a callback would vanish without a trace.
    root.report_callback_exception = lambda *error: journal.error("Unexpected error", exc_info=error)

    def say(text, tag="", to_file=True):
        if tag:  # lines are marked with the connection they belong to
            text = "\n".join(f"[{tag}] {line}" for line in text.splitlines())
        if to_file:
            for row in text.splitlines():
                journal.info(row)
        log.configure(state="normal")
        log.insert("end", text + "\n")
        log.see("end")
        log.configure(state="disabled")

    # Background work never touches Tk itself: its result goes through this queue and done() runs on Tk's
    # own thread. Calling Tk from a worker thread crashes Python ("PyEval_RestoreThread: the GIL is
    # released") when the main thread is inside a Win32 message loop such as the tray menu at that moment.
    finished = queue.SimpleQueue()

    def background(work, done):
        threading.Thread(target=lambda: finished.put((done, work())), daemon=True).start()

    def deliver():
        root.after(50, deliver)
        while not finished.empty():
            done, result = finished.get()
            try:
                done(result)
            except Exception:
                journal.exception("Unexpected error")

    root.after(50, deliver)

    def toggle(p):
        conn = conns[p["key"]]
        # grab_current: a dialog is open; the tray menu must not start a second action behind it.
        if conn["busy"] or conn["vpn"] is None or root.grab_current():
            return
        if conn["vpn"] == "off" and p.get("source") == "file":
            if p.get("otp_key") and p.get("username"):
                # shortcut: the code alone is sent as the password, add a password field if a server wants both.
                try:
                    typed = p["username"], totp(unprotect(p["otp_key"]))
                except Exception as err:  # the key was saved by another Windows user or on another machine
                    hint.configure(text=f"{label_of(p)}: the saved one-time code key cannot be read ({err}).")
                    return
            else:
                typed = ask_credentials(root, label_of(p), p.get("username", ""))
            if not typed:
                return
            if typed[0] != p.get("username"):  # remembered for the next time; the password never is
                p["username"] = typed[0]
                save_profiles(profiles)
            start(p["key"], "Connecting...", "",
                  ["-ProfilePath", p["profile_path"]], stdin_text=f"{typed[0]}\n{typed[1]}\n")
        elif conn["vpn"] == "off":
            start(p["key"], "Connecting...",
                  f"{label_of(p)}: approve the 1Password prompt." + ("" if WINDOWS else " Then the administrator prompt."),
                  ["-Vault", p["vault"], "-Item", p["item"], "-ProfileFile", p["profile_file"],
                   "-OtpField", p.get("otp_field") or "one-time password", "-OtpMode", p.get("otp_mode") or "Auto"])
        else:
            if not confirm(root, f"Disconnect {label_of(p)}?"):
                return
            link = links.get(p["key"])
            if link:  # free the management interface for the script's graceful stop
                try:
                    link.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            start(p["key"], "Disconnecting...", "", ["-Disconnect"])

    def save_profiles(items):
        save_json(PROFILES, items)

    def edit(p):
        """Adds (p is None), changes or deletes a profile, then rebuilds the cards in place."""
        if root.grab_current():
            return
        if p and conns[p["key"]]["vpn"] != "off":
            hint.configure(text=f"Disconnect {label_of(p)} before changing it.")
            return
        if any(conn["busy"] for conn in conns.values()):  # the cards are rebuilt after saving: not mid-action
            hint.configure(text="Wait until the running connect or disconnect has finished.")
            return
        answer = edit_profile(root, p, set())  # names are labels: they may repeat
        if not answer:
            return
        items = list(profiles)
        if answer[0] == "delete":
            if not confirm(root, f"Delete profile {label_of(p)}?"):
                return
            items.remove(p)
        elif p:
            # The number and the internal key belong to the connection, not to what the editor shows.
            items[items.index(p)] = {**answer[1], "id": p["id"], "key": p["key"]}
        else:
            items.append(answer[1])
            number_profiles(items)  # a new connection gets the next number, and that number as its key
        save_profiles(items)
        # The copy made on import holds keys: remove it once no profile points at it any more.
        imported = Path((p or {}).get("profile_path") or "-")
        if imported.parent == PROFILE_DIR and str(imported) not in {item.get("profile_path") for item in items}:
            imported.unlink(missing_ok=True)
        # The same for a one-time code key in the macOS Keychain (on Windows the key lives in the file only).
        old_key = (p or {}).get("otp_key")
        if not WINDOWS and old_key and old_key not in {item.get("otp_key") for item in items}:
            vpn_mac.forget(old_key)
        rebuild_cards(items)

    scale = display_scale(root)
    graph_size = round(170 * scale), round(44 * scale)

    # One card per connection: padlock indicator, name, status line, traffic graph and its own buttons.
    cards = {}

    def build_card(p):
        # The outer frame is the card's border and holds a stripe in the state colour along the left edge.
        shell = tk.Frame(frame, bg=ui["card"], highlightthickness=1, highlightbackground=ui["border"])
        # drag_move() re-packs the cards with the same options; cards added later go above "Add profile".
        shell.pack(fill="x", pady=(0, 10), **({"before": app["add_button"]} if "add_button" in app else {}))
        stripe = tk.Frame(shell, bg=ui["off"], width=max(round(5 * display_scale(root)), 3))
        stripe.pack(side="left", fill="y")
        # A grip on the left edge says the card can be dragged; the whole card drags, the grip shows it.
        grip = tk.Label(shell, text="⋮⋮", bg=ui["card"], fg=ui["muted"], font=("Segoe UI", 11), cursor="fleur",
                        padx=4)
        grip.pack(side="left", fill="y")
        card = tk.Frame(shell, bg=ui["card"], padx=10, pady=12)
        card.pack(side="left", fill="both", expand=True)
        # Room for the longest status line, so a long uptime is not clipped by the graph next to it.
        card.columnconfigure(1, weight=1, minsize=tkfont.Font(family="Segoe UI", size=10).measure(
            "Connected  ·  255.255.255.255  ·  99 h 59 min"))
        lamp = tk.Label(card, image=pictures["off"], bg=ui["card"])
        lamp.grid(row=0, column=0, rowspan=3, padx=(0, 14))
        # The name with a pencil after it: a click on the pencil or a double click on the name renames.
        name_row = tk.Frame(card, bg=ui["card"])
        name_row.grid(row=0, column=1, sticky="w")
        # The connection's number: it stays the same whatever the name is.
        tk.Label(name_row, text=f"#{p['id']}", bg=ui["card"], fg=ui["muted"], font=("Segoe UI Semibold", 13)).pack(
            side="left", padx=(0, 6))
        title = tk.Label(name_row, text=p["name"], bg=ui["card"], fg=ui["text"], font=("Segoe UI Semibold", 13),
                         cursor="xterm")
        title.pack(side="left")
        title.bind("<Double-Button-1>", lambda event, p=p: rename(p))
        pencil = tk.Label(name_row, text="✎", bg=ui["card"], fg=ui["muted"], font=("Segoe UI", 11), cursor="hand2")
        pencil.pack(side="left", padx=(6, 0))
        pencil.bind("<Button-1>", lambda event, p=p: rename(p))
        pencil.bind("<Enter>", lambda event: event.widget.configure(fg=ui["text"]))
        pencil.bind("<Leave>", lambda event: event.widget.configure(fg=ui["muted"]))
        line = tk.Label(card, text="Checking...", bg=ui["card"], fg=ui["off"], font=("Segoe UI", 10))
        line.grid(row=1, column=1, sticky="w")
        button = ttk.Button(card, text="Connect", width=13, command=lambda p=p: toggle(p))
        button.grid(row=0, column=3, rowspan=3, padx=(16, 0))
        edit_button = ttk.Button(card, text="Edit", width=5, command=lambda p=p: edit(p))
        edit_button.grid(row=0, column=4, rowspan=3, padx=(8, 0))
        # Traffic as a small table, download above upload. Every cell has a fixed width and the numbers are
        # right-aligned, so changing digits neither shift their neighbours nor resize the window.
        table = tk.Frame(card, bg=ui["card"])
        table.grid(row=2, column=1, sticky="w")
        flow = []
        for index in range(8):
            width, anchor = ((2, "w"), (11, "e"), (7, "e"), (10, "e"))[index % 4]
            cell = tk.Label(table, text="", bg=ui["card"], fg=ui["muted"], font=("Segoe UI", 9),
                            width=width, anchor=anchor, pady=0)
            cell.grid(row=index // 4, column=index % 4)
            flow.append(cell)
        graph = tk.Canvas(card, width=graph_size[0], height=graph_size[1], bg=ui["card"], highlightthickness=0)
        graph.grid(row=0, column=2, rowspan=3, padx=(16, 0))
        cards[p["key"]] = lamp, line, button, flow, graph, edit_button, stripe, shell, title
        # The card is dragged by any of its passive parts; the buttons keep their own clicks.
        for handle in (card, grip, lamp, name_row, title, line, table, graph, stripe, *flow):
            handle.bind("<ButtonPress-1>", lambda event, p=p: drag.update(profile=p, start=event.y_root, moved=False))
            handle.bind("<B1-Motion>", lambda event: drag_move(event.y_root))
            handle.bind("<ButtonRelease-1>", lambda event: drag_end())

    for p in profiles:
        build_card(p)

    add_button = app["add_button"] = ttk.Button(frame, text="+  Add profile", command=lambda: edit(None))
    add_button.pack(anchor="w", pady=(0, 6))

    def rebuild_cards(items):
        """Shows a changed profile list in place: no restart, so the window stays where it is."""
        for parts in cards.values():
            parts[7].destroy()  # the card's outer frame
        cards.clear()
        profiles[:] = items
        names[:] = [item["key"] for item in items]
        for key in [key for key in conns if key not in names]:
            del conns[key]
        for item in items:
            # A new profile starts unknown; an existing one keeps what is known about it.
            conns.setdefault(item["key"], {"vpn": None, "busy": "", "log": "", "traffic": None, "rates": []})
            build_card(item)
            update_card(item["key"])
        hint.configure(text="" if items else "No profiles yet: add one to get started.")
        check_now()

    # Reordering by drag and drop: the order is only how the cards are listed, so it is allowed at any time.
    # While a card is carried the others stay put: an accent line shows where it will land, and the move
    # happens on release.
    drag = {"profile": None, "start": 0, "moved": False, "position": None}
    drop_line = tk.Frame(frame, bg=BRAND["accent"], height=max(round(3 * display_scale(root)), 2))

    def drag_move(pointer_y):
        p = drag["profile"]
        if not p or (not drag["moved"] and abs(pointer_y - drag["start"]) < 8):
            return  # a click with a slightly shaky hand is not a drag
        if not drag["moved"]:
            drag["moved"] = True
            root.configure(cursor="fleur")
            # The card being carried gets an accent frame, so it is clear which one is moving.
            cards[p["key"]][7].configure(highlightbackground=BRAND["accent"], highlightcolor=BRAND["accent"],
                                         highlightthickness=2)
        # The landing place: before the first other card whose middle is below the pointer.
        others = [q for q in profiles if q is not p]
        position = sum(pointer_y > cards[q["key"]][7].winfo_rooty() + cards[q["key"]][7].winfo_height() / 2
                       for q in others)
        if position == drag["position"]:
            return
        drag["position"] = position
        drop_line.pack_forget()
        if position == profiles.index(p):
            return  # it would land where it is: no line
        anchor = cards[others[position]["key"]][7] if position < len(others) else add_button
        drop_line.pack(fill="x", pady=(0, 7), before=anchor)

    def drag_end():
        p, position = drag["profile"], drag["position"]
        if drag["moved"]:
            root.configure(cursor="")
            drop_line.pack_forget()
            if p["key"] in cards:
                cards[p["key"]][7].configure(highlightbackground=ui["border"], highlightcolor=ui["border"],
                                             highlightthickness=1)
            if position is not None and position != profiles.index(p):
                profiles.remove(p)
                profiles.insert(position, p)
                for q in profiles:
                    cards[q["key"]][7].pack_forget()
                for q in profiles:
                    cards[q["key"]][7].pack(fill="x", pady=(0, 10), before=add_button)
                save_profiles(profiles)
        drag.update(profile=None, moved=False, position=None)

    def rename(p):
        """Double click on the name: edit it in place. Enter or a click elsewhere keeps it, Escape gives up.

        The name is only a label, so it can change even while the VPN is connected.
        """
        old = p["name"]
        if root.grab_current() or drag["moved"]:
            return
        title = cards[p["key"]][8]
        text = tk.StringVar(value=old)
        # Laid over the name and its pencil, in the card's grid cell they share.
        entry = ttk.Entry(title.master.master, textvariable=text, font=("Segoe UI Semibold", 12), width=24)
        entry.grid(row=0, column=1, sticky="w")
        entry.select_range(0, "end")
        entry.focus_set()
        problem = tk.Label(entry.master, bg=ui["card"], fg=ui["danger"], font=("Segoe UI", 9))  # gridded on refusal

        def close():
            entry.destroy()
            problem.destroy()
        hint.configure(text="Enter or a click elsewhere saves the name, Esc cancels.")

        def keep(event=None):
            if not entry.winfo_exists():
                return  # already closed: Enter closes it, and the focus leaving afterwards calls this again
            try:
                new = check_label(text.get())
            except ValueError as error:
                hint.configure(text=f"Not renamed. {error}")
                if event is not None and event.type == tk.EventType.FocusOut:
                    close()  # the user went elsewhere: leave the old name, the line under the cards says why
                else:  # right under the field, where the user is looking, over the card's status line
                    problem.configure(text=str(error))
                    problem.grid(row=1, column=1, sticky="w")
                return
            if new != old:
                p["name"] = new
                title.configure(text=new)
                save_profiles(profiles)
            hint.configure(text="")
            close()

        def cancel(event=None):
            close()
            hint.configure(text="")

        entry.bind("<Return>", keep)
        entry.bind("<Escape>", cancel)
        entry.bind("<FocusOut>", keep)

    def draw_graph(name):
        """Download (green area) and upload (blue line) speed of the last two minutes, scaled to the highest value."""
        graph, rates = cards[name][4], conns[name]["rates"]
        graph.delete("all")
        width, height = graph_size
        graph.create_line(0, height - 1, width, height - 1, fill=ui["border"])  # baseline, also shown while idle
        if len(rates) < 2:
            return
        peak = max(max(pair) for pair in rates) or 1
        down = graph_points([pair[0] for pair in rates], width, height, peak)
        thick = max(round(1.5 * scale), 1)
        graph.create_polygon(down[0], height - 1, *down, down[-2], height - 1, fill=ui["fill"], outline="")
        graph.create_line(*down, fill=ui["on"], width=thick)
        graph.create_line(*graph_points([pair[1] for pair in rates], width, height, peak), fill=UPLOAD, width=thick)
        # The scale of the graph: its highest value in the window shown.
        label = graph.create_text(2, 0, anchor="nw", text=f"peak {format_bytes(peak)}/s", fill=ui["muted"],
                                  font=("Segoe UI", 7))
        _, _, right, bottom = graph.bbox(label)  # the backing starts at the left edge, where the lines start too
        graph.tag_lower(graph.create_rectangle(0, 0, right + 2, bottom, fill=ui["card"], outline=""), label)

    hint = ttk.Label(frame, text="" if profiles else "No profiles yet: add one to get started.",
                     foreground="gray", wraplength=360)
    hint.pack(anchor="w", pady=(0, 4))
    # Shown under the hint line only while an update is being downloaded: the progress and a way out.
    download_row = ttk.Frame(frame)
    download_bar = ttk.Progressbar(download_row, mode="determinate", maximum=100)
    download_bar.pack(side="left", fill="x", expand=True)
    ttk.Button(download_row, text="Cancel", command=lambda: app.update(cancel=True)).pack(side="right", padx=(10, 0))
    frame.bind("<Configure>", lambda event: hint.configure(wraplength=max(event.width - 32, 100)))

    def update_card(name):
        conn = conns[name]
        lamp, line, button = cards[name][:3]
        shown = conn["vpn"] or "off"
        lamp.configure(image=pictures["connecting" if conn["busy"] else shown])
        cards[name][6].configure(bg=ui["connecting" if conn["busy"] else shown])
        # The accent colour marks the action that starts something; Disconnect stays neutral.
        button.configure(text=conn["busy"] or ("Connect" if shown == "off" else "Disconnect"),
                         style="Accent.TButton" if shown == "off" and not conn["busy"] else "TButton")
        button.state(["disabled"] if conn["busy"] or conn["vpn"] is None else ["!disabled"])
        # A profile is edited only while it is fully disconnected.
        cards[name][5].state(["disabled"] if conn["busy"] or conn["vpn"] != "off" else ["!disabled"])

    def show_status(result):
        for name, (new, text, log_text) in result.items():
            if name not in conns:
                continue  # renamed while this check was running; the next one uses the new name
            conn = conns[name]
            previous, conn["vpn"] = conn["vpn"], new
            if log_text != conn["log"]:
                # Append only the new part of the OpenVPN log; a new connection starts the file over.
                fresh = log_text[len(conn["log"]):] if log_text.startswith(conn["log"]) else log_text
                conn["log"] = log_text
                # OpenVPN logs this program's own traffic reader as MANAGEMENT lines: noise for the user.
                fresh = "\n".join(row for row in fresh.splitlines() if " MANAGEMENT: " not in row)
                if fresh.strip():
                    # The first read after a start replays the whole OpenVPN log: shown, but not written again.
                    say(fresh.rstrip(), label_for(name), to_file=previous is not None)
            cards[name][1].configure(text=text, fg=ui[new])
            # Also while reconnecting: OpenVPN may be waiting on the link for the credentials.
            if new != "off" and not conn["busy"] and name not in links:
                links[name] = None  # reserved, so that the next refresh does not start a second watcher
                threading.Thread(target=watch_traffic, args=(name, links, samples, answer_password), daemon=True).start()
            if new != "on":
                conn["traffic"], conn["rates"] = None, []
                for cell in cards[name][3]:
                    cell.configure(text="")
                draw_graph(name)
            elif not conn["busy"]:
                sample, last = samples.get(name), conn["traffic"]
                if sample and sample != last:  # no new sample: the previous line stays
                    rate = traffic_rate(last and last[1:], sample[1:], sample[0] - last[0] if last else 0)
                    for cell, text in zip(cards[name][3], traffic_cells(rate, sample[1:])):
                        cell.configure(text=text)
                    conn["traffic"] = sample
                    if rate:
                        conn["rates"] = [*conn["rates"], rate][-GRAPH_POINTS:]
                        draw_graph(name)
            update_card(name)
            if previous and previous != new and (new == "on" or previous == "on"):
                notify(f"{label_for(name)}: VPN connected" if new == "on" else f"{label_for(name)}: VPN disconnected", text, new)
        up = sum(conn["vpn"] == "on" for conn in conns.values())
        summary.configure(text=f"{up} of {len(conns)} connected" if up else "Not connected",
                          fg=BRAND["on"] if up else BRAND["muted"])
        overall = overall_state([conn["vpn"] for conn in conns.values()])
        if overall != app["overall"]:
            app["overall"] = overall
            set_window_icon(root, overall)  # window and taskbar icon follow the state
        update_tray()  # after notify: it restores the small tray icon

    def check_now():
        # One check at a time: when one is slow, overlapping ones would apply their results out of order
        # (a state flickering on and off, notifications twice). The next tick simply tries again.
        if app.get("polling"):
            return
        app["polling"] = True

        def checked(result):
            app["polling"] = False
            if isinstance(result, Exception):
                journal.error("Status check failed", exc_info=result)
            else:
                show_status(result)

        def check():
            try:
                return vpn_status(names)
            except Exception as error:  # a failed check must not leave "polling" set for good
                return error

        background(check, checked)

    def refresh():
        root.after(REFRESH_MS, refresh)  # first, so that an error below cannot stop the polling for good
        # The window's place and size are remembered as they change, so even a reboot does not lose them.
        if root.state() == "normal" and root.geometry() != settings.get("geometry"):
            settings["geometry"] = root.geometry()
            save_json(SETTINGS, settings)
        # The colours are taken at start-up, so a theme change (in the settings, or in Windows with "Auto")
        # restarts the program: now if nothing is going on, otherwise on a later tick.
        if resolve_theme(settings["theme"]) != theme and not root.grab_current() and restart():
            return
        check_now()

    def finish(name, result):
        code, text = result
        say(text or f"exit code {code}", label_for(name))
        conns[name]["busy"] = ""
        update_card(name)
        if code == 0:
            hint.configure(text="")
        else:
            hint.configure(text=f"{label_for(name)}: failed. See the log below.")
            checks["show_log"].set(True)
            on_toggle("show_log")
        check_now()

    def start(name, busy, tip, args, stdin_text=""):
        conns[name]["busy"] = busy
        update_card(name)
        hint.configure(text=tip)
        say(busy, label_for(name))
        background(lambda: run_script("-Name", name, *args, stdin_text=stdin_text),
                   lambda result: finish(name, result))

    def answer_password(name, link):
        """Watcher thread: OpenVPN asks for the credentials again (the server renegotiates keys, or a restart).

        The one-time code from the first sign-in is spent, so a fresh one is sent. Returns True when the link
        was closed for the connect script, which answers 1Password profiles itself.
        """
        p = next((p for p in profiles if p["key"] == name), None)

        def tell(text):
            finished.put((lambda _: say(text, label_for(name)), None))

        if not p:
            return False
        if p.get("source") != "file":
            tell("The server asked to sign in again: getting a fresh code from 1Password...")
            # shutdown, not close: the reader's makefile keeps the socket open until it is closed itself.
            link.shutdown(socket.SHUT_RDWR)
            code, text = run_script("-Name", name, "-Reauth", "-Vault", p["vault"], "-Item", p["item"],
                                    "-OtpField", p.get("otp_field") or "one-time password",
                                    "-OtpMode", p.get("otp_mode") or "Auto")
            tell(text or f"exit code {code}")
            return True
        try:
            if not (p.get("otp_key") and p.get("username")):
                raise ValueError("this profile has no saved one-time code key")
            fresh = totp(unprotect(p["otp_key"]))
        except Exception as err:
            # shortcut: a typed password is not kept, so the tunnel is stopped; ask for it here if that is too often.
            tell(f"The server asked to sign in again, but {err}: disconnecting. Connect again.")
            link.sendall(b"signal SIGTERM\n")
            return False
        link.sendall(f'username "Auth" {management_argument(p["username"])}\n'
                     f'password "Auth" {management_argument(fresh)}\n'.encode())
        tell("The server asked to sign in again: a fresh code was sent.")
        return False

    def look_for_update(asked=False):
        """Asks GitHub for a newer release; asked=True when the user pressed the button and wants an answer."""
        def shown(found):
            if found and found != app["update"]:
                if app.get("update_button"):
                    app["update_button"].destroy()
                app["update_button"] = pill_button(header, f"Update to {found[0]}", install_update, primary=True)
                app["update_button"].pack(side="right", padx=(0, 10))
            app["update"] = found
            if app.get("downloading"):
                return  # the hint line is showing the download progress: leave it alone
            if found:
                if asked:
                    hint.configure(text=f"Version {found[0]} is available: press the update button at the top.")
            elif asked:
                hint.configure(text="This is a development copy, it does not update itself." if not parse_version(VERSION)
                               else f"Version {VERSION} is the latest one, or the update server cannot be reached.")
        background(check_update, shown)

    def automatic_update_check():
        root.after(UPDATE_CHECK_MS, automatic_update_check)
        if settings["check_updates"]:
            look_for_update()

    def install_update():
        """Downloads the new installer, checks it and hands over to it; VPN connections keep running."""
        update = app["update"]
        if not update or root.grab_current() or app.get("downloading"):
            return
        if any(conn["busy"] for conn in conns.values()):
            hint.configure(text="Wait until the running connect or disconnect has finished.")
            return
        if not confirm(root, f"Download and install version {update[0]}?\n\n"
                             "VPN connections stay up. The program closes and the installer takes over."):
            return
        app["downloading"] = True  # a second click must not start a second download
        app["progress"] = (0, 0)
        hint.configure(text=f"Downloading version {update[0]}...")
        app["cancel"] = False
        download_bar.configure(value=0)
        download_row.pack(fill="x", pady=(0, 8), after=hint)

        def show_progress():
            # The download thread only stores the numbers; the label is updated here, on Tk's own thread.
            if app["downloading"]:
                done, total = app["progress"]
                hint.configure(text=f"Downloading version {update[0]}: {progress_text(done, total)}")
                download_bar.configure(value=done * 100 / total if total else 0)
                root.after(100, show_progress)

        def progress(done, total):
            # Called by the download thread for every chunk: the place where a cancel request stops it.
            if app["cancel"]:
                raise DownloadCancelled()
            app["progress"] = done, total

        def download():
            try:
                return download_update(update, progress)
            except Exception as error:  # anything at all: an uncaught one would leave the download "running"
                return error

        root.after(300, show_progress)

        def downloaded(result):
            app["downloading"] = False
            download_row.pack_forget()
            if isinstance(result, DownloadCancelled):
                hint.configure(text=f"The download of version {update[0]} was cancelled.")
                return
            if isinstance(result, Exception):
                hint.configure(text=f"The update could not be downloaded: {result}")
                return
            say(f"Starting the installer of version {update[0]}: {result}")
            if WINDOWS:
                # /SILENT: no wizard pages. The installer asks for elevation itself and restarts the program.
                # A result of 32 or less is a failure, including "No" in the elevation prompt: then stay open.
                if ctypes.windll.shell32.ShellExecuteW(None, "open", str(result), "/SILENT", None, 1) <= 32:
                    hint.configure(text=f"The installer of version {update[0]} was not started.")
                    return
                release_instance()  # the installer must not find the program still running
                quit_app()
            else:
                # shortcut: on macOS the disk image is opened for the usual drag to Applications.
                subprocess.run(["open", str(result)])
                hint.configure(text="Quit VPN Launcher, then drag the new version to Applications.")

        background(download, downloaded)

    def about():
        if not root.grab_current():
            show_about(root, lambda: look_for_update(asked=True))

    # Settings as checkboxes; every change is saved and applied immediately.
    box = ttk.LabelFrame(frame, text="Settings", padding=(12, 6))
    box.pack(fill="x", pady=(6, 0))
    box.columnconfigure((0, 1), weight=1, uniform="settings")
    checks = {}

    def on_toggle(key):
        settings[key] = checks[key].get()
        save_json(SETTINGS, settings)
        if key == "autostart":
            set_autostart(settings[key])
        if key == "show_log":
            if settings[key]:
                log_box.pack(fill="both", expand=True, pady=(12, 0))
            else:
                log_box.pack_forget()
            root.geometry("")  # back to the natural size, a hand-set size would clip or leave a gap

    for number, (key, label, _) in enumerate(CHECKBOXES):  # switches in two columns
        checks[key] = tk.BooleanVar(value=bool(settings[key]))
        ttk.Checkbutton(box, text=label, variable=checks[key], style="Switch.TCheckbutton",
                        command=lambda key=key: on_toggle(key)).grid(row=number // 2, column=number % 2, sticky="w")

    def on_theme(event):
        settings["theme"] = THEMES[theme_choice.get()]
        save_json(SETTINGS, settings)
        restart()  # if an action is running, refresh() does it once that has finished

    theme_row = ttk.Frame(box)
    theme_row.grid(row=len(CHECKBOXES) // 2, column=len(CHECKBOXES) % 2, sticky="w", pady=(2, 4))
    ttk.Label(theme_row, text="Theme").pack(side="left", padx=(4, 10))
    theme_choice = ttk.Combobox(theme_row, state="readonly", width=8, values=list(THEMES))
    theme_choice.set(next((label for label, value in THEMES.items() if value == settings["theme"]), "Auto"))
    theme_choice.bind("<<ComboboxSelected>>", on_theme)
    theme_choice.pack(side="left")
    root.update_idletasks()
    root.minsize(root.winfo_reqwidth(), root.winfo_reqheight())  # never smaller than the controls
    if settings["show_log"]:
        log_box.pack(fill="both", expand=True, pady=(12, 0))
    if on_screen(settings.get("geometry")):  # not when that monitor is gone
        root.geometry(settings["geometry"])

    def show_window():
        root.deiconify()
        root.lift()
        root.focus_force()

    def quit_app():
        remove_tray()
        root.destroy()

    def restart():
        # shortcut: a profile change restarts the program instead of rebuilding the cards, running VPNs are not touched.
        if any(conn["busy"] for conn in conns.values()):
            return False  # a connect or disconnect script is running: a restart would leave it without an owner
        remove_tray()
        subprocess.Popen([*SELF, "--show"], creationflags=NO_WINDOW)
        root.destroy()
        return True

    def menu_items():
        """(text, action, enabled) rows; every connection has its own connect or disconnect row."""
        items = []
        for p in profiles:
            conn = conns[p["key"]]
            verb = "Disconnect" if conn["vpn"] in ("connecting", "on") else "Connect"
            items.append((f"{verb} {label_of(p)}", lambda p=p: toggle(p), not conn["busy"] and conn["vpn"] is not None))
        return items + [("Open window", show_window, True), ("About", about, True), ("Exit", quit_app, True)]

    def tray_tip():
        words = {None: "checking", "off": "off", "connecting": "connecting", "on": "connected"}
        return "VPN — " + ", ".join(f"{label_for(name)}: {words[conn['vpn']]}" for name, conn in conns.items())

    # The icon next to the clock: gray, orange or green padlock by state, with a menu of the connections.
    # Each platform defines update_tray(), notify(title, text, state) and remove_tray().
    if WINDOWS:
        # The icon's hidden window lives on a thread of its own with its own message loop. On Tk's thread the
        # menu's modal loop (TrackPopupMenu) ran Tk's timers inside a Win32 callback, and their Python
        # callbacks killed the interpreter ("PyEval_RestoreThread: the GIL is released"); reproduced by
        # choosing About from the menu while the window was hidden. This thread never calls Tk: what the user
        # picks goes through the same queue as background results, and Tk's thread acts on it.
        # shortcut: the icon is not re-added after an Explorer restart, handle TaskbarCreated if that matters.
        def tray(*call):
            """Shell_NotifyIcon that tolerates a missing icon: after an Explorer restart every call would raise."""
            try:
                win32gui.Shell_NotifyIcon(*call)
            except win32gui.error:
                pass

        def on_tk_thread(action):
            finished.put((lambda _: action(), None))

        def tray_menu():
            items = menu_items()  # only reads the shared state
            menu = win32gui.CreatePopupMenu()
            for number, (text, _, enabled) in enumerate(items, 1):
                flags = win32con.MF_STRING | (0 if enabled else win32con.MF_GRAYED)
                win32gui.AppendMenu(menu, flags, number, text)
            try:
                win32gui.SetForegroundWindow(tray_hwnd)  # otherwise the menu stays open after a click outside
            except win32gui.error:
                pass  # Windows may refuse it; the menu still works, it just closes less eagerly
            x, y = win32gui.GetCursorPos()
            chosen = win32gui.TrackPopupMenu(menu, win32con.TPM_RETURNCMD | win32con.TPM_NONOTIFY,
                                             x, y, 0, tray_hwnd, None)
            win32gui.DestroyMenu(menu)
            if chosen:
                on_tk_thread(items[chosen - 1][1])

        def on_tray(hwnd, msg, wparam, lparam):
            if lparam == win32con.WM_LBUTTONUP:
                on_tk_thread(show_window)
            elif lparam == win32con.WM_RBUTTONUP:
                tray_menu()
            return 0

        tray_ready = threading.Event()

        def run_tray_window():
            nonlocal tray_hwnd
            window_class = win32gui.WNDCLASS()
            window_class.hInstance = win32api.GetModuleHandle(None)
            window_class.lpszClassName = "VpnLauncherTray"
            window_class.lpfnWndProc = {TRAY_MSG: on_tray}
            tray_hwnd = win32gui.CreateWindow(win32gui.RegisterClass(window_class), "", 0, 0, 0, 0, 0, 0, 0,
                                              window_class.hInstance, None)
            tray_ready.set()
            win32gui.PumpMessages()

        tray_hwnd = None
        threading.Thread(target=run_tray_window, daemon=True).start()
        tray_ready.wait(10)
        icons = {name: load_icon(name, win32con.SM_CXSMICON) for name in COLORS}
        big_icons = {name: load_icon(name, win32con.SM_CXICON) for name in COLORS}

        def tray_data():
            flags = win32gui.NIF_ICON | win32gui.NIF_MESSAGE | win32gui.NIF_TIP
            return tray_hwnd, 0, flags, TRAY_MSG, icons[app["overall"] or "off"], tray_tip()[:127]

        def update_tray():
            tray(win32gui.NIM_MODIFY, tray_data())

        def notify(title, text, state):
            if settings["notify"]:
                tray(win32gui.NIM_MODIFY, (
                    tray_hwnd, 0, win32gui.NIF_INFO | win32gui.NIF_ICON, TRAY_MSG, big_icons[state], "",
                    text[:255], 10, title, NIIF_USER_LARGE))

        def remove_tray():
            tray(win32gui.NIM_DELETE, (tray_hwnd, 0))

        tray(win32gui.NIM_ADD, tray_data())
    else:
        # Menu bar item; its actions are handed to Tk's loop like the tray menu's on Windows.
        status_item = vpn_mac.StatusItem(
            lambda: [(text, lambda action=action: root.after(0, action), enabled)
                     for text, action, enabled in menu_items()],
            lambda state: str(icon_path(state).with_suffix(".png")))

        def update_tray():
            status_item.update(app["overall"] or "off", tray_tip())

        def notify(title, text, state):
            if settings["notify"]:
                vpn_mac.notify(title, text)

        remove_tray = status_item.remove
        root.createcommand("tk::mac::ReopenApplication", show_window)  # a click on the Dock icon
        root.createcommand("tk::mac::Quit", quit_app)  # Cmd-Q and Quit in the Dock menu
    root.protocol("WM_DELETE_WINDOW", lambda: root.withdraw() if settings["close_to_tray"] else quit_app())
    if settings["start_minimized"] and "--show" not in sys.argv:  # --show: restarted after a profile change
        root.withdraw()
    if "--smoke" in sys.argv:  # used by the build pipeline: the window must come up and close without an error
        root.report_callback_exception = lambda *error: (journal.error("Smoke test", exc_info=error), os._exit(1))
        root.after(5000, quit_app)

    # Installers of earlier updates have done their job: do not let them pile up in the temp folder.
    for leftover in (WORK / "update").glob("*"):
        try:
            leftover.unlink()
        except OSError:
            pass  # still in use by an installer that is finishing
    root.after(4000, automatic_update_check)
    refresh()
    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # The windowed build has no console: leave the reason for a failed start where it can be found.
        import traceback
        DATA.mkdir(parents=True, exist_ok=True)
        (DATA / "crash.log").write_text(f"{time.ctime()}\n{traceback.format_exc()}", encoding="utf-8")
        raise
