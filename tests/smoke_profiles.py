"""Prepares the build machine for the smoke run: writes two sample profiles into the program's data folder.

Importing vpn_launcher also runs the assertions it carries, so this doubles as the self-check step.
Only meant for a throwaway build machine: it overwrites the profile list.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import vpn_launcher  # noqa: E402

vpn_launcher.DATA.mkdir(parents=True, exist_ok=True)
vpn_launcher.PROFILES.write_text(json.dumps([
    {"name": "Sample-1Password", "vault": "Vault", "item": "Item", "profile_file": "sample.ovpn"},
    {"name": "Sample-File", "source": "file", "profile_path": "sample.ovpn", "username": "user"},
]), encoding="utf-8")
print("self-checks passed; sample profiles written to", vpn_launcher.PROFILES)
