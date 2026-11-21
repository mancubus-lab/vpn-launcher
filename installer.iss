; Inno Setup script of VPN Launcher. Built by build.ps1, which passes AppVersion and IconFile.
;
; Setup asks for administrator rights once. With them it installs the program for all users, installs the
; OpenVPN community client when it is missing, and lets the installing user start VPN connections through
; the OpenVPN interactive service, so that connecting never asks for elevation again.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{6C0E7F0B-5E0A-4C59-9B0E-7B1E4B1B7A11}
AppName=VPN Launcher
AppVersion={#AppVersion}
DefaultDirName={autopf}\VPN Launcher
DefaultGroupName=VPN Launcher
DisableProgramGroupPage=yes
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=dist
OutputBaseFilename=VpnLauncherSetup-{#AppVersion}
SetupIconFile={#IconFile}
UninstallDisplayIcon={app}\VPN Launcher.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"
Name: "opcli"; Description: "Install the 1Password CLI (only for profiles that take credentials from 1Password)"; Flags: unchecked

[Files]
Source: "dist\VPN Launcher\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\VPN Launcher"; Filename: "{app}\VPN Launcher.exe"
Name: "{autodesktop}\VPN Launcher"; Filename: "{app}\VPN Launcher.exe"; Tasks: desktopicon

[Registry]
; The program adds this autostart value itself; it is only removed here on uninstall.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: none; ValueName: "VpnLauncher"; Flags: uninsdeletevalue dontcreatekey

[Run]
; Prerequisites come through winget, which ships with Windows 10 and 11.
Filename: "{cmd}"; Parameters: "/c winget install --id OpenVPNTechnologies.OpenVPN -e --silent --accept-package-agreements --accept-source-agreements"; StatusMsg: "Installing the OpenVPN client..."; Flags: runhidden; Check: not OpenVpnInstalled
Filename: "{cmd}"; Parameters: "/c winget install --id AgileBits.1Password.CLI -e --silent --accept-package-agreements --accept-source-agreements"; StatusMsg: "Installing the 1Password CLI..."; Flags: runhidden; Tasks: opcli
; The OpenVPN interactive service takes free-form options only from Administrators and from this group.
; shortcut: {username} is the account that approved the elevation; when an administrator installs for a
; standard user, add that user to the group by hand.
Filename: "{sys}\net.exe"; Parameters: "localgroup ""OpenVPN Administrators"" /add"; Flags: runhidden
Filename: "{sys}\net.exe"; Parameters: "localgroup ""OpenVPN Administrators"" ""{username}"" /add"; Flags: runhidden
Filename: "{app}\VPN Launcher.exe"; Parameters: "--show"; Description: "Start VPN Launcher"; Flags: nowait postinstall skipifsilent runasoriginaluser

[Code]
function OpenVpnInstalled: Boolean;
begin
  Result := FileExists(ExpandConstant('{commonpf64}\OpenVPN\bin\openvpn.exe'));
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and not OpenVpnInstalled then
    MsgBox('The OpenVPN client could not be installed automatically.' + #13#10 +
           'Install "OpenVPN Community" from openvpn.net, then start VPN Launcher.', mbInformation, MB_OK);
end;
