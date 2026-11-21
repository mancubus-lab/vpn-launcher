"""macOS side of VPN Launcher.

Everything the Windows build does through Win32, PowerShell and the OpenVPN interactive service:
starting and stopping OpenVPN, the Keychain, autostart, the menu bar item and notifications.

OpenVPN needs root on macOS and there is no service to delegate to, so every connect shows the
standard administrator prompt. Because OpenVPN then runs as root on a profile the user can edit,
profiles that use directives which run programs or write files are refused outright.
"""
import base64
import fcntl
import json
import os
import plistlib
import re
import shlex
import shutil
import socket
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

BUNDLE_ID = "co.mancubus.vpnlauncher"
DATA = Path.home() / "Library" / "Application Support" / "VpnLauncher"
WORK = Path(tempfile.gettempdir()) / "vpn_connect_op"  # the per-user temp folder on macOS
AGENT = Path.home() / "Library" / "LaunchAgents" / f"{BUNDLE_ID}.plist"
KEYCHAIN_SERVICE = "VPN Launcher"
# An app started from Finder gets a bare PATH; Homebrew lives outside it.
SEARCH_PATH = os.pathsep.join([os.environ.get("PATH", ""), "/opt/homebrew/bin", "/opt/homebrew/sbin",
                               "/usr/local/bin", "/usr/local/sbin"])
TIMEOUT_SECONDS = 90


class VpnError(Exception):
    """A failure with a message meant for the user."""


def find_tool(name):
    return shutil.which(name, path=SEARCH_PATH)


# ---------------------------------------------------------------------------------------------- small helpers

def format_password(password, otp, mode):
    """How the one-time code travels: appended to the password, or as an SCRV1 static-challenge answer."""
    if mode == "None":
        return password
    if mode == "Scrv1":
        encode = lambda text: base64.b64encode(text.encode()).decode()  # noqa: E731
        return f"SCRV1:{encode(password)}:{encode(otp)}"
    return password + otp


assert format_password("p", "1", "Scrv1") == "SCRV1:cA==:MQ=="
assert format_password("p", "1", "Append") == "p1" and format_password("p", "1", "None") == "p"


def parse_arguments(arguments):
    """The launcher passes the same "-Flag value" list it gives the PowerShell script on Windows."""
    options, index = {}, 0
    while index < len(arguments):
        flag = arguments[index].lstrip("-")
        if flag == "Disconnect":
            options[flag], index = True, index + 1
        else:
            options[flag], index = arguments[index + 1], index + 2
    return options


assert parse_arguments(["-Name", "Lab", "-Disconnect"]) == {"Name": "Lab", "Disconnect": True}
assert parse_arguments(["-Name", "Lab", "-ProfilePath", "/a b.ovpn"]) == {"Name": "Lab", "ProfilePath": "/a b.ovpn"}


def applescript_string(text):
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


assert applescript_string('a "b" \\c') == '"a \\"b\\" \\\\c"'


def run_as_root(command):
    """Runs a shell command as root through the standard macOS administrator prompt."""
    script = (f"do shell script {applescript_string(command)} with administrator privileges "
              'with prompt "VPN Launcher needs administrator rights to control OpenVPN."')
    done = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if done.returncode != 0:
        raise VpnError(f"The administrator prompt was cancelled or failed: {done.stderr.strip()}")


def write_private(path, text):
    """Creates the file readable by its owner only, before any secret is written to it."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)


def openvpn_pids():
    """PIDs (as strings) of every running openvpn process."""
    listing = subprocess.run(["ps", "-axo", "pid=,comm="], capture_output=True, text=True).stdout
    return {line.split(None, 1)[0] for line in listing.splitlines() if line.strip().endswith("openvpn")}


def read_pid(folder):
    try:
        return (folder / "pid.txt").read_text().strip()
    except OSError:
        return ""


def is_running(folder):
    pid = read_pid(folder)
    return bool(pid) and pid in openvpn_pids()


def find_openvpn(say):
    """Path of the openvpn binary; installs it with Homebrew when it is missing and Homebrew is there."""
    found = find_tool("openvpn")
    if found:
        return found
    brew = find_tool("brew")
    if not brew:
        raise VpnError("OpenVPN is not installed. Install Homebrew from https://brew.sh, "
                       "then run: brew install openvpn")
    say("OpenVPN is not installed: installing it with Homebrew, this can take a few minutes...")
    done = subprocess.run([brew, "install", "openvpn"], capture_output=True, text=True, timeout=1200)
    found = find_tool("openvpn")
    if not found:
        raise VpnError(f"Homebrew could not install OpenVPN:\n{done.stderr.strip()[-800:]}")
    return found


def one_password(*arguments):
    """Runs the 1Password CLI and returns its output without the trailing newline."""
    tool = find_tool("op")
    if not tool:
        raise VpnError("The 1Password CLI (op) is not installed. Run: brew install 1password-cli")
    done = subprocess.run([tool, *arguments], capture_output=True, text=True)
    if done.returncode != 0:
        raise VpnError(f"1Password: {done.stderr.strip()}")
    return done.stdout.rstrip("\n")


def stop_gracefully(folder):
    """Asks OpenVPN to exit over its management interface (no root needed); True when it is gone."""
    try:
        port = int((folder / "port.txt").read_text())
        password = (folder / "mgmt.txt").read_text().split()[0]
        with socket.create_connection(("127.0.0.1", port), timeout=3) as link:
            link.sendall(f"{password}\nsignal SIGTERM\n".encode())
            time.sleep(0.5)
    except (OSError, ValueError, IndexError):
        return False
    for _ in range(20):
        if not is_running(folder):
            return True
        time.sleep(0.5)
    return False


def stop(folder):
    """Stops the connection of this folder; returns True when the stop had to be forced."""
    forced = not stop_gracefully(folder)
    if forced and is_running(folder):
        run_as_root(f"kill {int(read_pid(folder))}")
        time.sleep(1)
        if is_running(folder):
            raise VpnError("OpenVPN could not be stopped.")
    for name in ("mgmt.txt", "port.txt", "pid.txt"):
        (folder / name).unlink(missing_ok=True)
    return forced


# ---------------------------------------------------------------------------------------- connect / disconnect

def connect(options, stdin_text, say, risky_directives):
    name = options["Name"]
    folder = WORK / name
    folder.mkdir(parents=True, exist_ok=True)
    if is_running(folder):
        raise VpnError(f"{name} is already running. Disconnect it first.")
    openvpn = find_openvpn(say)
    log, credentials, management = folder / "openvpn.log", folder / "creds.txt", folder / "mgmt.txt"
    credentials.unlink(missing_ok=True)  # left behind if an earlier run was killed before its cleanup
    from_1password = "ProfilePath" not in options
    profile = folder / "profile.ovpn" if from_1password else Path(options["ProfilePath"])
    state = "timeout"
    try:
        if from_1password:
            vault = options["Vault"]
            # Secret references reject characters such as ':' in item names, so the item is addressed by ID.
            item = json.loads(one_password(
                "item", "get", options["Item"], "--vault", vault, "--format", "json"))["id"]
            one_password("read", f"op://{vault}/{item}/{options['ProfileFile']}", "--out-file", str(profile), "--force")
            mode = options.get("OtpMode", "Auto")
            if mode == "Auto":
                challenge = re.search(r"^\s*static-challenge", profile.read_text(errors="replace"), re.MULTILINE)
                mode = "Scrv1" if challenge else "Append"
            user = one_password("read", f"op://{vault}/{item}/username")
            password = one_password("read", f"op://{vault}/{item}/password")
            # The code is fetched last so that it is as fresh as possible when the server checks it.
            field = options.get("OtpField") or "one-time password"
            code = one_password("read", f"op://{vault}/{item}/{field}?attribute=otp") if mode != "None" else ""
            secret, note = format_password(password, code, mode), f"OTP mode was {mode}"
        else:
            user, _, rest = stdin_text.partition("\n")
            secret, note = rest.split("\n")[0], "the password was typed by the user"
            if not user:
                raise VpnError(f"{name}: no user name was given.")

        risky = risky_directives(profile.read_text(errors="replace"))
        if risky:
            raise VpnError(f"{name}: this profile uses directives that run programs or write files "
                           f"({', '.join(risky)}). OpenVPN runs as root on macOS, so such a profile is refused.")

        write_private(credentials, f"{user}\n{secret}\n")
        write_private(management, uuid.uuid4().hex + "\n")
        # Created here, as the user: OpenVPN (root) then truncates it instead of making its own root-only file.
        log.unlink(missing_ok=True)
        log.touch()
        (folder / "pid.txt").unlink(missing_ok=True)
        with socket.socket() as probe:  # a free port picked by the OS
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        (folder / "port.txt").write_text(str(port))

        # --script-security 1 comes after --config and therefore wins: a profile cannot make OpenVPN run scripts.
        command = [openvpn, "--config", profile, "--script-security", "1", "--auth-user-pass", credentials,
                   "--log", log, "--management", "127.0.0.1", port, management,
                   "--writepid", folder / "pid.txt", "--daemon"]
        run_as_root(" ".join(shlex.quote(str(part)) for part in command))

        deadline = time.monotonic() + TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            time.sleep(0.5)
            text = log.read_text(errors="replace")
            if "Initialization Sequence Completed" in text:
                state = "connected"
                break
            if "AUTH_FAILED" in text:
                state = f"auth failed ({note})"
                break
            if read_pid(folder) and not is_running(folder):
                state = "openvpn exited"
                break
    finally:
        credentials.unlink(missing_ok=True)
        if from_1password:
            profile.unlink(missing_ok=True)

    if state != "connected":
        if log.exists():
            say("\n".join(log.read_text(errors="replace").splitlines()[-20:]))
        # Do not leave OpenVPN retrying in the background with a one-time code that is already spent.
        if is_running(folder):
            stop(folder)
        raise VpnError(f"{name} is not connected: {state}. Full log: {log}")
    say(f"{name}: connected.")


def disconnect(options, say):
    name = options["Name"]
    folder = WORK / name
    if not is_running(folder):
        say(f"{name}: not connected.")
        return
    forced = stop(folder)
    say(f"{name}: disconnected (forced: the graceful stop was not available)." if forced else f"{name}: disconnected.")


def run(arguments, stdin_text, risky_directives):
    """Entry point used by the launcher; returns (exit code, output) and never raises."""
    lines = []
    try:
        options = parse_arguments(list(arguments))
        if options.get("Disconnect"):
            disconnect(options, lines.append)
        else:
            connect(options, stdin_text, lines.append, risky_directives)
        return 0, "\n".join(lines)
    except VpnError as error:
        return 1, "\n".join([*lines, str(error)])
    except (OSError, subprocess.SubprocessError, KeyError, ValueError) as error:
        return 1, "\n".join([*lines, f"Unexpected error: {error!r}"])


# ------------------------------------------------------------------------------------------- system integration

def protect(secret):
    """Stores a secret in the login Keychain and returns the handle to keep in the profile list."""
    handle = uuid.uuid4().hex
    # Passed over stdin: a command line would be visible to every process of the user.
    done = subprocess.run(["security", "-i"], capture_output=True, text=True,
                          input=f'add-generic-password -U -a {handle} -s "{KEYCHAIN_SERVICE}" -w "{secret}"\n')
    if done.returncode != 0:
        raise OSError(f"The Keychain refused the key: {done.stderr.strip()}")
    return handle


def unprotect(handle):
    done = subprocess.run(["security", "find-generic-password", "-a", handle, "-s", KEYCHAIN_SERVICE, "-w"],
                          capture_output=True, text=True)
    if done.returncode != 0:
        raise OSError(f"The key is not in the Keychain: {done.stderr.strip()}")
    return done.stdout.strip()


def system_theme():
    """ "dark" when macOS is in dark mode; the default value does not exist in light mode."""
    done = subprocess.run(["defaults", "read", "-g", "AppleInterfaceStyle"], capture_output=True, text=True)
    return "dark" if done.stdout.strip() == "Dark" else "light"


def set_autostart(enabled, command):
    """A LaunchAgent starts the program at login."""
    if not enabled:
        AGENT.unlink(missing_ok=True)
        return
    AGENT.parent.mkdir(parents=True, exist_ok=True)
    with open(AGENT, "wb") as handle:
        plistlib.dump({"Label": BUNDLE_ID, "ProgramArguments": list(command), "RunAtLoad": True}, handle)


def first_instance(wait):
    """True when this process may run. The lock is held until the process ends.

    wait: a restarted instance gives the previous one a few seconds to exit.
    """
    global _instance_lock
    DATA.mkdir(parents=True, exist_ok=True)
    _instance_lock = open(DATA / "instance.lock", "w")
    for _ in range(16 if wait else 1):
        try:
            fcntl.flock(_instance_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            time.sleep(0.5)
    # shortcut: the running instance is not brought to the front, click its Dock or menu bar icon.
    return False


def notify(title, text):
    subprocess.run(["osascript", "-e", f"display notification {applescript_string(text)} "
                                       f"with title {applescript_string(title)}"], capture_output=True)


class StatusItem:
    """Menu bar icon with a menu that is rebuilt every time it opens.

    menu_items() returns (title, action, enabled) rows; actions are called on the main thread.
    Tk already runs the Cocoa event loop on macOS, so the item works without a loop of its own.
    """

    def __init__(self, menu_items, icon_file):
        from AppKit import NSImage, NSMenu, NSMenuItem, NSStatusBar, NSVariableStatusItemLength
        from Foundation import NSObject

        class Target(NSObject):
            def menuNeedsUpdate_(self, menu):
                menu.removeAllItems()
                self.rows = menu_items()
                for number, (title, _, enabled) in enumerate(self.rows):
                    row = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, "fire:", "")
                    row.setTarget_(self)
                    row.setTag_(number)
                    row.setEnabled_(bool(enabled))
                    menu.addItem_(row)

            def fire_(self, sender):
                self.rows[sender.tag()][1]()

        self._image, self._icon_file = NSImage, icon_file
        self._target = Target.alloc().init()
        self._target.rows = []
        self._item = NSStatusBar.systemStatusBar().statusItemWithLength_(NSVariableStatusItemLength)
        menu = NSMenu.alloc().init()
        menu.setAutoenablesItems_(False)
        menu.setDelegate_(self._target)
        self._item.setMenu_(menu)
        self._menu = menu  # the status item does not keep its delegate alive
        self.update("off", "VPN Launcher")

    def update(self, state, tip):
        image = self._image.alloc().initWithContentsOfFile_(self._icon_file(state))
        image.setSize_((18, 18))
        self._item.button().setImage_(image)
        self._item.button().setToolTip_(tip)

    def remove(self):
        from AppKit import NSStatusBar
        NSStatusBar.systemStatusBar().removeStatusItem_(self._item)
