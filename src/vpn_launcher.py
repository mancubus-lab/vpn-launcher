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
import json
import logging
import logging.handlers
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
import uuid
import winreg
from pathlib import Path
from tkinter import filedialog, font as tkfont, messagebox, ttk

import sv_ttk
import win32api
import win32con
import win32crypt
import win32event
import win32gui
from PIL import Image, ImageDraw, ImageTk

FROZEN = getattr(sys, "frozen", False)  # True in the installed build made by PyInstaller
# Bundled files sit next to this file, or in PyInstaller's resource folder in the installed build.
RESOURCES = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
# Profiles, settings and imported .ovpn files live in the user profile, never next to the code:
# Program Files is read-only for the user, and a source checkout must not collect personal data.
DATA = Path(os.environ["APPDATA"]) / "VpnLauncher"
# How to start this program again (autostart entry, restart after a profile or theme change).
SELF = [sys.executable] if FROZEN else [str(Path(sys.executable).with_name("pythonw.exe")), str(Path(__file__).resolve())]
SCRIPT = str(RESOURCES / "vpn_connect_op.ps1")
PROFILES = DATA / "vpn_launcher_profiles.json"
PROFILE_DIR = DATA / "vpn_launcher_profiles"  # imported .ovpn files
GRAPH_POINTS = 60 # traffic samples kept for the graph: two minutes at one sample per 2 s
SETTINGS = DATA / "vpn_launcher_settings.json"
WORK = Path(os.environ["TEMP"]) / "vpn_connect_op"  # the script keeps one subfolder per connection here
JOURNAL = WORK / "launcher.log"  # copy of the window's journal for later troubleshooting
JOURNAL_BYTES = 1_000_000  # per file; one older file (launcher.log.1) is kept, so at most twice this on disk
ICONS = DATA / "vpn_launcher_icons"  # generated on first use
PWSH = shutil.which("pwsh") or "powershell"
NO_WINDOW = subprocess.CREATE_NO_WINDOW
REFRESH_MS = 2000
TRAY_MSG = win32con.WM_USER + 20
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APP_ID = "VpnLauncher"
COLORS = {"off": "#7a7a7a", "connecting": "#e08a00", "on": "#2e9e44"}
# Colours of the hand-drawn parts (cards, graph, log) per theme; "fill" is the tint under the download line.
PALETTES = {
    "light": {"card": "#ffffff", "border": "#e4e4e4", "muted": "#6b6b6b", "fill": "#d9f1de",
              "text": "#1a1a1a", "log": "#333333"},
    "dark": {"card": "#232323", "border": "#383838", "muted": "#a6a6a6", "fill": "#1f4a2a",
             "text": "#ffffff", "log": "#d4d4d4"},
}
UPLOAD = "#3b7dd8"
NIIF_USER_LARGE = 0x4 | 0x20  # NIIF_USER | NIIF_LARGE_ICON: the notification shows our icon, not the blue "i"
# Setting key, checkbox label, default value.
CHECKBOXES = [
    ("autostart", "Start with Windows", False),
    ("start_minimized", "Start minimized to tray", False),
    ("close_to_tray", "Close button minimizes to tray", True),
    ("notify", "Show connection notifications", True),
    ("show_log", "Show log", False),
]
THEMES = {"Auto": "auto", "Light": "light", "Dark": "dark"}  # label in the settings -> stored value


def icon_path(state):
    """Draws a white padlock on a rounded square in the state colour; the .ico is cached in ICONS."""
    path = ICONS / f"lock_{state}.ico"
    if not path.with_suffix(".png").exists():
        ICONS.mkdir(parents=True, exist_ok=True)
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
        image.save(path.with_suffix(".png"))  # full-size master for the picture inside the window
    return path


def load_icon(state, metric):
    size = win32api.GetSystemMetrics(metric)
    return win32gui.LoadImage(0, str(icon_path(state)), win32con.IMAGE_ICON, size, size, win32con.LR_LOADFROMFILE)


def register_app_id():
    """Makes Windows label notifications "VPN Launcher" instead of "Python"."""
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"Software\Classes\AppUserModelId\{APP_ID}") as key:
        winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, "VPN Launcher")
        winreg.SetValueEx(key, "IconUri", 0, winreg.REG_SZ, str(icon_path("on")))
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)


def run_script(*args, stdin_text=""):
    """Runs the connect script; returns (exit code, output). Never raises: the caller's busy state depends on it."""
    try:
        return _run_script(*args, stdin_text=stdin_text)
    except OSError as err:
        return 1, f"PowerShell could not be started: {err}"


def _run_script(*args, stdin_text):
    proc = subprocess.run(
        # Bypass: a fresh Windows blocks every script by default; this only covers our own bundled script.
        [PWSH, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", SCRIPT, *args],
        input=stdin_text, capture_output=True, text=True, encoding="utf-8", errors="replace",
        creationflags=NO_WINDOW,
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


def traffic_text(rate, totals):
    """Current speed (when known) plus the totals of the session."""
    total = f"total  ↓ {format_bytes(totals[0])}   ↑ {format_bytes(totals[1])}"
    return f"↓ {format_bytes(rate[0])}/s   ↑ {format_bytes(rate[1])}/s   ·   {total}" if rate else total


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
    return base64.b64encode(win32crypt.CryptProtectData(text.encode(), "VPN Launcher", None, None, None, 0)).decode()


def unprotect(blob):
    return win32crypt.CryptUnprotectData(base64.b64decode(blob), None, None, None, 0)[1].decode()


assert unprotect(protect("round trip")) == "round trip"


def check_profile(profile):
    """Raises ValueError when a profile entry cannot be used."""
    name = profile.get("name", "")
    if not re.fullmatch(r"[\w-]+", name):  # the name becomes a folder name in the script
        raise ValueError(f"Name {name!r}: use only letters, digits, _ and -")
    keys = ("profile_path",) if profile.get("source") == "file" else ("vault", "item", "profile_file")
    for key in keys:
        if not profile.get(key):
            raise ValueError(f"{name}: {key.replace('_', ' ')} is empty")


# Profile directives that make OpenVPN run a program, load code or write a file of the profile's choosing.
RISKY_DIRECTIVES = {
    "up", "down", "route-up", "route-pre-down", "ipchange", "tls-verify", "auth-user-pass-verify",
    "client-connect", "client-disconnect", "learn-address", "tls-crypt-v2-verify", "plugin",
    "pkcs11-providers", "providers", "management", "status", "writepid", "log", "log-append",
}


def risky_directives(profile_text):
    """Sorted names of the RISKY_DIRECTIVES an .ovpn uses; inline <ca>...</ca> style blocks are skipped."""
    found, inside = set(), False
    for line in profile_text.splitlines():
        words = line.split()
        if not words:
            continue
        if words[0].startswith("</"):
            inside = False
        elif words[0].startswith("<"):
            inside = True
        elif not inside and words[0].lower().lstrip("-") in RISKY_DIRECTIVES:
            found.add(words[0].lower().lstrip("-"))
    return sorted(found)


assert risky_directives("client\nUP 'cmd /c x'\n<ca>\nplugin not-a-directive\n</ca>\n  plugin x.dll\n# up\n") == [
    "plugin", "up"]
assert risky_directives("client\nremote vpn.example 1194\nscript-security 2\n") == []


def load_profiles():
    """Profiles from the JSON file; no file means a fresh install with nothing configured yet."""
    if not PROFILES.exists():
        return []
    profiles = json.loads(PROFILES.read_text(encoding="utf-8"))
    for profile in profiles:
        check_profile(profile)
    # Compared without case: the name is a folder name, and Windows folders ignore case.
    if len({p["name"].casefold() for p in profiles}) != len(profiles):
        raise ValueError("profile names must be unique")
    return profiles


assert format_bytes(512) == "512 B" and format_bytes(1536) == "1.5 KB" and format_bytes(3 * 1024 ** 3) == "3.00 GB"
assert parse_traffic(">BYTECOUNT:2048,10\n") == (2048, 10)
assert parse_traffic("ENTER PASSWORD:SUCCESS: password is correct") is None
assert traffic_rate((0, 0), (2048, 1024), 2) == (1024, 512) and traffic_rate(None, (1, 1), 2) is None
assert traffic_text((1024, 512), (2048, 1024)) == "↓ 1.0 KB/s   ↑ 512 B/s   ·   total  ↓ 2.0 KB   ↑ 1.0 KB"
assert traffic_text(None, (2048, 1024)) == "total  ↓ 2.0 KB   ↑ 1.0 KB"
assert graph_points([0, 10], 59, 24, 10) == [0, 22, 1, 2]
check_profile({"name": "Lab", "source": "file", "profile_path": "x.ovpn"})
for bad in ({"name": "a b", "vault": "v", "item": "i", "profile_file": "f"}, {"name": "Lab", "source": "file"}):
    try:
        check_profile(bad)
        raise AssertionError(bad)
    except ValueError:
        pass


def watch_traffic(name, links, samples):
    """Thread body: holds one management link open and stores the counters OpenVPN pushes every 2 s.

    One long-lived link instead of polling, because OpenVPN logs every management connection and command.
    The link is the only one OpenVPN accepts, so it must be closed before the script stops the connection.
    """
    try:
        port = int((WORK / name / "port.txt").read_text())
        password = (WORK / name / "mgmt.txt").read_text().split()[0]
        with socket.create_connection(("127.0.0.1", port), timeout=5) as link:
            links[name] = link
            link.settimeout(None)
            link.sendall(f"{password}\nbytecount 2\n".encode())
            for line in link.makefile("r", errors="replace"):
                traffic = parse_traffic(line)
                if traffic:
                    samples[name] = (time.monotonic(), *traffic)
    except (OSError, ValueError, IndexError):
        pass  # not reachable or closed: the caller starts a new watcher on the next refresh
    finally:
        links.pop(name, None)
        samples.pop(name, None)


def vpn_status(names):
    """Returns {name: (state, text, log_text)}; a connection is alive while the PID the script saved is an openvpn.exe."""
    tasks = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq openvpn.exe", "/NH", "/FO", "CSV"],
        capture_output=True, text=True, creationflags=NO_WINDOW,
    ).stdout
    pids = set(re.findall(r'^"openvpn\.exe","(\d+)"', tasks, re.MULTILINE | re.IGNORECASE))
    result = {}
    for name in names:
        pid_file, log_file = WORK / name / "pid.txt", WORK / name / "openvpn.log"
        try:
            alive = pid_file.read_text().strip() in pids
            uptime = time.time() - pid_file.stat().st_mtime  # the file is written when openvpn.exe starts
        except OSError:
            alive, uptime = False, 0
        log_text = log_file.read_text(errors="replace") if log_file.exists() else ""
        result[name] = (*parse_status(alive, log_text, uptime), log_text)
    return result


def load_settings():
    settings = {key: default for key, _, default in CHECKBOXES}
    settings["theme"] = "auto"
    try:
        settings.update(json.loads(SETTINGS.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass  # first run or a damaged file: fall back to the defaults
    return settings


def resolve_theme(choice):
    """"light" or "dark"; "auto" (and anything unknown) follows the Windows app mode."""
    if choice in ("light", "dark"):
        return choice
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
    left, top, width, height = (ctypes.windll.user32.GetSystemMetrics(index) for index in (76, 77, 78, 79))
    x, y = int(found.group(1)), int(found.group(2))
    return left <= x < left + width - 100 and top <= y < top + height - 100


assert not on_screen("") and not on_screen("800x600+999999+0") and not on_screen("garbage")


def set_autostart(enabled):
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
    box.geometry(f"+{x}+{y}")
    box.deiconify()
    box.lift()
    box.grab_set()
    focus.focus_force()
    root.wait_window(box)


def match_title_bar(window):
    """Makes the Windows title bar dark when the dark theme is on; it does not follow the ttk theme."""
    window.update_idletasks()
    dark = ctypes.c_int(sv_ttk.get_theme() == "dark")
    ctypes.windll.dwmapi.DwmSetWindowAttribute(  # 20 = DWMWA_USE_IMMERSIVE_DARK_MODE
        ctypes.windll.user32.GetParent(window.winfo_id()), 20, ctypes.byref(dark), ctypes.sizeof(dark))


def new_dialog(root):
    box = tk.Toplevel(root)
    box.withdraw()  # hidden until show_modal has placed it; otherwise it flashes at a default spot first
    box.title("VPN Launcher")
    box.configure(bg=ttk.Style().lookup("TFrame", "background"))  # a Toplevel does not follow the ttk theme
    match_title_bar(box)
    box.resizable(False, False)
    box.iconbitmap(icon_path("on"))
    return box


def field(parent, row, label, variable, **options):
    """A labelled entry on one grid row; returns the entry."""
    ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=3)
    entry = ttk.Entry(parent, textvariable=variable, width=36, **options)
    entry.grid(row=row, column=1, sticky="we", pady=3)
    return entry


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
        ttk.Label(panels["file"], text="Base32 secret of the authenticator; stored encrypted for this Windows user.",
                  foreground="gray").grid(row=3, column=1, columnspan=2, sticky="w")

    error = ttk.Label(body, text="", foreground="#c0392b", wraplength=420)
    error.grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))

    def save():
        source = values["source"].get()
        keys = ("profile_path", "username") if source == "file" else (
            "vault", "item", "profile_file", "otp_field", "otp_mode")
        new = {"name": values["name"].get().strip(), "source": source,
               **{key: values[key].get().strip() for key in keys}}
        try:
            check_profile(new)
            if new["name"].casefold() in taken:
                raise ValueError(f"{new['name']}: this name is already used")
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


def main():
    # Without this Windows bitmap-scales the window on a scaled display and it looks blurry.
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
    if not first_instance():
        return
    DATA.mkdir(parents=True, exist_ok=True)
    register_app_id()
    root = tk.Tk()
    root.title("VPN Launcher")
    root.iconbitmap(icon_path("off"))
    settings = load_settings()
    theme = resolve_theme(settings["theme"])
    ui = PALETTES[theme]
    sv_ttk.set_theme(theme)  # Windows 11 look for the ttk widgets
    root.configure(bg=ttk.Style().lookup("TFrame", "background"))
    match_title_bar(root)
    # The theme sizes its fonts in pixels, which stay tiny on a scaled display.
    for font_name in tkfont.names(root):
        if font_name.startswith("SunValley"):
            theme_font = tkfont.nametofont(font_name)
            theme_font.configure(size=round(theme_font.cget("size") * root.winfo_fpixels("1i") / 96))

    try:
        profiles = load_profiles()
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as err:
        messagebox.showerror("VPN Launcher", f"Cannot read {PROFILES}:\n{err}")
        return
    names = [p["name"] for p in profiles]

    # Per connection: vpn is a COLORS key (None until the first check), busy is the label of a running action.
    # traffic is the last sample: (time, bytes in, bytes out).
    # rates is the history for the graph: (bytes per second down, up).
    conns = {name: {"vpn": None, "busy": "", "log": "", "traffic": None, "rates": []} for name in names}
    app = {"overall": None}
    links, samples = {}, {}  # per connection: the open management link and the newest (time, in, out)

    frame = ttk.Frame(root, padding=16)
    frame.pack(fill="both", expand=True)
    # The same padlock as in the tray and the title bar, sized for the display scale.
    side = round(36 * root.winfo_fpixels("1i") / 96)
    pictures = {name: ImageTk.PhotoImage(Image.open(icon_path(name).with_suffix(".png")).resize((side, side)))
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

    def background(work, done):
        threading.Thread(target=lambda: root.after(0, done, work()), daemon=True).start()

    def toggle(p):
        conn = conns[p["name"]]
        # grab_current: a dialog is open; the tray menu must not start a second action behind it.
        if conn["busy"] or conn["vpn"] is None or root.grab_current():
            return
        if conn["vpn"] == "off" and p.get("source") == "file":
            if p.get("otp_key") and p.get("username"):
                # shortcut: the code alone is sent as the password, add a password field if a server wants both.
                try:
                    typed = p["username"], totp(unprotect(p["otp_key"]))
                except Exception as err:  # the key was saved by another Windows user or on another machine
                    hint.configure(text=f"{p['name']}: the saved one-time code key cannot be read ({err}).")
                    return
            else:
                typed = ask_credentials(root, p["name"], p.get("username", ""))
            if not typed:
                return
            if typed[0] != p.get("username"):  # remembered for the next time; the password never is
                p["username"] = typed[0]
                save_profiles(profiles)
            start(p["name"], "Connecting...", "",
                  ["-ProfilePath", p["profile_path"]], stdin_text=f"{typed[0]}\n{typed[1]}\n")
        elif conn["vpn"] == "off":
            start(p["name"], "Connecting...",
                  f"{p['name']}: approve the 1Password prompt.",
                  ["-Vault", p["vault"], "-Item", p["item"], "-ProfileFile", p["profile_file"],
                   "-OtpField", p.get("otp_field") or "one-time password", "-OtpMode", p.get("otp_mode") or "Auto"])
        else:
            if not confirm(root, f"Disconnect {p['name']}?"):
                return
            link = links.get(p["name"])
            if link:  # free the management interface for the script's graceful stop
                try:
                    link.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            start(p["name"], "Disconnecting...", "", ["-Disconnect"])

    def save_profiles(items):
        # Written to a side file and swapped in: a crash mid-write must not leave a truncated profile list.
        draft = PROFILES.with_suffix(".tmp")
        draft.write_text(json.dumps(items, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(draft, PROFILES)

    def edit(p):
        """Adds (p is None), changes or deletes a profile, then restarts the program to rebuild the cards."""
        if root.grab_current():
            return
        if p and conns[p["name"]]["vpn"] != "off":
            hint.configure(text=f"Disconnect {p['name']} before changing it.")
            return
        if any(conn["busy"] for conn in conns.values()):  # the restart after saving would orphan that action
            hint.configure(text="Wait until the running connect or disconnect has finished.")
            return
        answer = edit_profile(root, p, {name.casefold() for name in names if not p or name != p["name"]})
        if not answer:
            return
        items = list(profiles)
        if answer[0] == "delete":
            if not confirm(root, f"Delete profile {p['name']}?"):
                return
            items.remove(p)
        elif p:
            items[items.index(p)] = answer[1]
        else:
            items.append(answer[1])
        save_profiles(items)
        # The copy made on import holds keys: remove it once no profile points at it any more.
        imported = Path((p or {}).get("profile_path") or "-")
        if imported.parent == PROFILE_DIR and str(imported) not in {item.get("profile_path") for item in items}:
            imported.unlink(missing_ok=True)
        restart()

    scale = root.winfo_fpixels("1i") / 96
    graph_size = round(170 * scale), round(44 * scale)

    # One card per connection: padlock indicator, name, status line, traffic graph and its own buttons.
    cards = {}
    for p in profiles:
        card = tk.Frame(frame, bg=ui["card"], highlightthickness=1, highlightbackground=ui["border"], padx=16, pady=12)
        card.pack(fill="x", pady=(0, 10))
        card.columnconfigure(1, weight=1)
        lamp = tk.Label(card, image=pictures["off"], bg=ui["card"])
        lamp.grid(row=0, column=0, rowspan=3, padx=(0, 14))
        tk.Label(card, text=p["name"], bg=ui["card"], fg=ui["text"], font=("Segoe UI Semibold", 13)).grid(row=0, column=1, sticky="w")
        line = tk.Label(card, text="Checking...", bg=ui["card"], fg=COLORS["off"], font=("Segoe UI", 10))
        line.grid(row=1, column=1, sticky="w")
        button = ttk.Button(card, text="Connect", width=13, command=lambda p=p: toggle(p))
        button.grid(row=0, column=3, rowspan=3, padx=(16, 0))
        edit_button = ttk.Button(card, text="Edit", width=5, command=lambda p=p: edit(p))
        edit_button.grid(row=0, column=4, rowspan=3, padx=(8, 0))
        flow = tk.Label(card, text="", bg=ui["card"], fg=ui["muted"], font=("Segoe UI", 9))
        flow.grid(row=2, column=1, sticky="w")
        graph = tk.Canvas(card, width=graph_size[0], height=graph_size[1], bg=ui["card"], highlightthickness=0)
        graph.grid(row=0, column=2, rowspan=3, padx=(16, 0))
        cards[p["name"]] = lamp, line, button, flow, graph, edit_button

    ttk.Button(frame, text="+  Add profile", command=lambda: edit(None)).pack(anchor="w", pady=(0, 6))

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
        graph.create_line(*down, fill=COLORS["on"], width=thick)
        graph.create_line(*graph_points([pair[1] for pair in rates], width, height, peak), fill=UPLOAD, width=thick)

    hint = ttk.Label(frame, text="" if profiles else "No profiles yet: add one to get started.",
                     foreground="gray", wraplength=360)
    hint.pack(anchor="w", pady=(0, 4))
    frame.bind("<Configure>", lambda event: hint.configure(wraplength=max(event.width - 32, 100)))

    def update_card(name):
        conn = conns[name]
        lamp, line, button = cards[name][:3]
        shown = conn["vpn"] or "off"
        lamp.configure(image=pictures["connecting" if conn["busy"] else shown])
        # The accent colour marks the action that starts something; Disconnect stays neutral.
        button.configure(text=conn["busy"] or ("Connect" if shown == "off" else "Disconnect"),
                         style="Accent.TButton" if shown == "off" and not conn["busy"] else "TButton")
        button.state(["disabled"] if conn["busy"] or conn["vpn"] is None else ["!disabled"])
        # A profile is edited only while it is fully disconnected.
        cards[name][5].state(["disabled"] if conn["busy"] or conn["vpn"] != "off" else ["!disabled"])

    def show_status(result):
        for name, (new, text, log_text) in result.items():
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
                    say(fresh.rstrip(), name, to_file=previous is not None)
            cards[name][1].configure(text=text, fg=COLORS[new])
            if new != "on":
                conn["traffic"], conn["rates"] = None, []
                cards[name][3].configure(text="")
                draw_graph(name)
            elif not conn["busy"]:
                if name not in links:
                    links[name] = None  # reserved, so that the next refresh does not start a second watcher
                    threading.Thread(target=watch_traffic, args=(name, links, samples), daemon=True).start()
                sample, last = samples.get(name), conn["traffic"]
                if sample and sample != last:  # no new sample: the previous line stays
                    rate = traffic_rate(last and last[1:], sample[1:], sample[0] - last[0] if last else 0)
                    cards[name][3].configure(text=traffic_text(rate, sample[1:]))
                    conn["traffic"] = sample
                    if rate:
                        conn["rates"] = [*conn["rates"], rate][-GRAPH_POINTS:]
                        draw_graph(name)
            update_card(name)
            if previous and previous != new and (new == "on" or previous == "on"):
                notify(f"{name}: VPN connected" if new == "on" else f"{name}: VPN disconnected", text, new)
        overall = overall_state([conn["vpn"] for conn in conns.values()])
        if overall != app["overall"]:
            app["overall"] = overall
            root.iconbitmap(icon_path(overall))  # window and taskbar icon follow the state
        update_tray()  # after notify: it restores the small tray icon

    def check_now():
        background(lambda: vpn_status(names), show_status)

    def refresh():
        root.after(REFRESH_MS, refresh)  # first, so that an error below cannot stop the polling for good
        # The window's place and size are remembered as they change, so even a reboot does not lose them.
        if root.state() == "normal" and root.geometry() != settings.get("geometry"):
            settings["geometry"] = root.geometry()
            SETTINGS.write_text(json.dumps(settings, indent=2), encoding="utf-8")
        # The colours are taken at start-up, so a theme change (in the settings, or in Windows with "Auto")
        # restarts the program: now if nothing is going on, otherwise on a later tick.
        if resolve_theme(settings["theme"]) != theme and not root.grab_current() and restart():
            return
        check_now()

    def finish(name, result):
        code, text = result
        say(text or f"exit code {code}", name)
        conns[name]["busy"] = ""
        update_card(name)
        if code == 0:
            hint.configure(text="")
        else:
            hint.configure(text=f"{name}: failed. See the log below.")
            checks["show_log"].set(True)
            on_toggle("show_log")
        check_now()

    def start(name, busy, tip, args, stdin_text=""):
        conns[name]["busy"] = busy
        update_card(name)
        hint.configure(text=tip)
        say(busy, name)
        background(lambda: run_script("-Name", name, *args, stdin_text=stdin_text),
                   lambda result: finish(name, result))

    # Settings as checkboxes; every change is saved and applied immediately.
    box = ttk.LabelFrame(frame, text="Settings", padding=(12, 6))
    box.pack(fill="x", pady=(6, 0))
    box.columnconfigure((0, 1), weight=1, uniform="settings")
    checks = {}

    def on_toggle(key):
        settings[key] = checks[key].get()
        SETTINGS.write_text(json.dumps(settings, indent=2), encoding="utf-8")
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
        SETTINGS.write_text(json.dumps(settings, indent=2), encoding="utf-8")
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

    # Tray icon: gray, orange or green padlock by state. Tk's event loop also pumps this window.
    # shortcut: the icon is not re-added after an Explorer restart, handle TaskbarCreated if that matters.
    def show_window():
        root.deiconify()
        root.lift()
        root.focus_force()

    def tray(*call):
        """Shell_NotifyIcon that tolerates a missing icon: after an Explorer restart every call would raise."""
        try:
            win32gui.Shell_NotifyIcon(*call)
        except win32gui.error:
            pass

    def quit_app():
        tray(win32gui.NIM_DELETE, (tray_hwnd, 0))
        root.destroy()

    def restart():
        # shortcut: a profile change restarts the program instead of rebuilding the cards, running VPNs are not touched.
        if any(conn["busy"] for conn in conns.values()):
            return False  # a connect or disconnect script is running: a restart would leave it without an owner
        tray(win32gui.NIM_DELETE, (tray_hwnd, 0))
        subprocess.Popen([*SELF, "--show"], creationflags=NO_WINDOW)
        root.destroy()
        return True

    def menu_items():
        """(text, action, enabled) rows; every connection has its own connect or disconnect row."""
        items = []
        for p in profiles:
            conn = conns[p["name"]]
            verb = "Disconnect" if conn["vpn"] in ("connecting", "on") else "Connect"
            items.append((f"{verb} {p['name']}", lambda p=p: toggle(p), not conn["busy"] and conn["vpn"] is not None))
        return items + [("Open window", show_window, True), ("Exit", quit_app, True)]

    def tray_menu():
        items = menu_items()
        menu = win32gui.CreatePopupMenu()
        for number, (text, _, enabled) in enumerate(items, 1):
            flags = win32con.MF_STRING | (0 if enabled else win32con.MF_GRAYED)
            win32gui.AppendMenu(menu, flags, number, text)
        win32gui.SetForegroundWindow(tray_hwnd)  # otherwise the menu stays open after a click outside
        x, y = win32gui.GetCursorPos()
        chosen = win32gui.TrackPopupMenu(menu, win32con.TPM_RETURNCMD | win32con.TPM_NONOTIFY,
                                         x, y, 0, tray_hwnd, None)
        win32gui.DestroyMenu(menu)
        if chosen:
            root.after(0, items[chosen - 1][1])

    def on_tray(hwnd, msg, wparam, lparam):
        if lparam == win32con.WM_LBUTTONUP:
            show_window()
        elif lparam == win32con.WM_RBUTTONUP:
            tray_menu()
        return 0

    window_class = win32gui.WNDCLASS()
    window_class.hInstance = win32api.GetModuleHandle(None)
    window_class.lpszClassName = "VpnLauncherTray"
    window_class.lpfnWndProc = {TRAY_MSG: on_tray}
    tray_hwnd = win32gui.CreateWindow(win32gui.RegisterClass(window_class), "", 0, 0, 0, 0, 0, 0, 0,
                                      window_class.hInstance, None)
    icons = {name: load_icon(name, win32con.SM_CXSMICON) for name in COLORS}
    big_icons = {name: load_icon(name, win32con.SM_CXICON) for name in COLORS}

    def tray_data():
        words = {None: "checking", "off": "off", "connecting": "connecting", "on": "connected"}
        tip = ", ".join(f"{name}: {words[conn['vpn']]}" for name, conn in conns.items())
        flags = win32gui.NIF_ICON | win32gui.NIF_MESSAGE | win32gui.NIF_TIP
        return tray_hwnd, 0, flags, TRAY_MSG, icons[app["overall"] or "off"], f"VPN — {tip}"[:127]

    def update_tray():
        tray(win32gui.NIM_MODIFY, tray_data())

    def notify(title, text, state):
        if settings["notify"]:
            tray(win32gui.NIM_MODIFY, (
                tray_hwnd, 0, win32gui.NIF_INFO | win32gui.NIF_ICON, TRAY_MSG, big_icons[state], "",
                text[:255], 10, title, NIIF_USER_LARGE))

    tray(win32gui.NIM_ADD, tray_data())
    root.protocol("WM_DELETE_WINDOW", lambda: root.withdraw() if settings["close_to_tray"] else quit_app())
    if settings["start_minimized"] and "--show" not in sys.argv:  # --show: restarted after a profile change
        root.withdraw()

    refresh()
    root.mainloop()


if __name__ == "__main__":
    main()
