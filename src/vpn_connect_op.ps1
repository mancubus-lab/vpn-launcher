<#
.SYNOPSIS
Connects the OpenVPN community client using a profile, credentials and TOTP stored in 1Password.

.DESCRIPTION
Run from a NON-elevated shell: the 1Password CLI talks to the desktop app as the current user.
openvpn.exe is started through the OpenVPN interactive service, so it runs as the user and there is no
elevation prompt; nothing here ever runs elevated. Scripts named in a profile are blocked. The profile and
credentials are written to a temp folder for the start of the connection and removed as soon as it is up
or has failed.

Every connection has a name and its own folder, log and management port under
%TEMP%\vpn_connect_op\<Name>, so several connections run side by side and are stopped one by one.

.EXAMPLE
.\vpn_connect_op.ps1 -Name Prod -Vault Work -Item "Prod VPN" -ProfileFile prod.ovpn

.EXAMPLE
# Without 1Password: the profile is a file, the user name and the password come from stdin.
"user", "password" | .\vpn_connect_op.ps1 -Name Lab -ProfilePath C:\vpn\lab.ovpn

.EXAMPLE
.\vpn_connect_op.ps1 -Name Prod -Status

.EXAMPLE
.\vpn_connect_op.ps1 -Name Prod -Disconnect

.EXAMPLE
# Profile stored as a separate Document item, OTP sent via static-challenge.
.\vpn_connect_op.ps1 -Name Work -Vault Work -Item "Prod VPN" -ProfileItem "Prod VPN profile" -ProfileFile prod.ovpn -OtpMode Scrv1
#>
[CmdletBinding(DefaultParameterSetName = "Connect")]
param(
    # Used as a folder name, hence the restricted character set.
    [Parameter(Mandatory = $true)][ValidatePattern('^[\w-]+$')][string]$Name,
    [Parameter(Mandatory = $true, ParameterSetName = "Connect")][string]$Vault,
    [Parameter(Mandatory = $true, ParameterSetName = "Connect")][string]$Item,
    [Parameter(Mandatory = $true, ParameterSetName = "Connect")][string]$ProfileFile,
    [Parameter(ParameterSetName = "Connect")][string]$ProfileItem = $Item,
    [Parameter(ParameterSetName = "Connect")][string]$OtpField = "one-time password",
    # Auto: Scrv1 when the profile declares static-challenge, otherwise Append (password + code).
    [Parameter(ParameterSetName = "Connect")][ValidateSet("Auto", "Append", "Scrv1", "None")][string]$OtpMode = "Auto",
    # Without 1Password: a profile file on disk; the user name and the password are read from stdin (two lines).
    [Parameter(Mandatory = $true, ParameterSetName = "File")][string]$ProfilePath,
    [Parameter(ParameterSetName = "Connect")][Parameter(ParameterSetName = "File")][int]$TimeoutSec = 90,
    [Parameter(Mandatory = $true, ParameterSetName = "Status")][switch]$Status,
    [Parameter(Mandatory = $true, ParameterSetName = "Disconnect")][switch]$Disconnect
)

$ErrorActionPreference = "Stop"
$OpenVpn = "C:\Program Files\OpenVPN\bin\openvpn.exe"

$work = Join-Path $env:TEMP "vpn_connect_op\$Name"
$log = Join-Path $work "openvpn.log"
$mgmtPw = Join-Path $work "mgmt.txt"    # random per-connection password of the management interface
$portFile = Join-Path $work "port.txt"  # management port on 127.0.0.1, used for a graceful stop
$pidFile = Join-Path $work "pid.txt"    # PID of the openvpn.exe of this connection

# Native commands (op) print UTF-8; without this a non-ASCII password or user name arrives garbled.
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)

function Read-OpSecret([string]$Ref) {
    $value = & op read $Ref
    if ($LASTEXITCODE -ne 0) { throw "op read failed for $Ref" }
    return $value
}

# Secret references reject characters such as ':' in item names, so items are addressed by ID.
function Resolve-OpItemId([string]$ItemName) {
    $json = & op item get $ItemName --vault $Vault --format json
    if ($LASTEXITCODE -ne 0) { throw "op item get failed for $ItemName" }
    return ($json | ConvertFrom-Json).id
}

# The openvpn.exe of this connection, or nothing. The name check guards against a reused PID.
function Get-VpnProcess {
    $id = Get-Content $pidFile -TotalCount 1 -ErrorAction SilentlyContinue
    if ($id) { Get-Process -Id $id -ErrorAction SilentlyContinue | Where-Object ProcessName -eq "openvpn" }
}

# Asks OpenVPN to exit through its management interface, so that it removes its own routes.
# A force-kill leaves them on the adapter and the next connection logs "route exists".
function Stop-VpnGracefully {
    try {
        $client = [Net.Sockets.TcpClient]::new("127.0.0.1", [int](Get-Content $portFile -TotalCount 1))
        $writer = [IO.StreamWriter]::new($client.GetStream())
        $writer.NewLine = "`n"
        $writer.AutoFlush = $true
        $writer.WriteLine((Get-Content $mgmtPw -TotalCount 1))
        $writer.WriteLine("signal SIGTERM")
        Start-Sleep -Milliseconds 500
        $client.Close()
    }
    catch { return $false }
    foreach ($attempt in 1..20) {
        if (-not (Get-VpnProcess)) { return $true }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

# Starts openvpn.exe through the OpenVPN interactive service (the mechanism OpenVPN GUI uses) and returns its PID.
# The service does the privileged work, so there is no elevation prompt. Free-form options are accepted only
# from members of the Administrators or "OpenVPN Administrators" group; everyone else gets an error here.
function Start-VpnViaService([string]$Options) {
    $pipe = [IO.Pipes.NamedPipeClientStream]::new(".", "openvpn\service", [IO.Pipes.PipeDirection]::InOut)
    try {
        $pipe.Connect(3000)
        $pipe.ReadMode = [IO.Pipes.PipeTransmissionMode]::Message
        # Startup message: working directory, options and stdin text, each ended by a NUL, as UTF-16.
        $bytes = [Text.Encoding]::Unicode.GetBytes("$work`0$Options`0`0")
        $pipe.Write($bytes, 0, $bytes.Length)
        $buffer = [byte[]]::new(4096)
        $count = $pipe.Read($buffer, 0, $buffer.Length)
        # Reply: error code, then the PID (or error details), one per line, numbers in hex.
        $reply = [Text.Encoding]::Unicode.GetString($buffer, 0, $count) -split "`n"
    }
    finally { $pipe.Dispose() }
    if ([Convert]::ToUInt32($reply[0].Trim(), 16) -ne 0) { throw "the service answered: $($reply -join ' ')" }
    return [int][Convert]::ToUInt32($reply[1].Trim(), 16)
}

function Format-VpnPassword([string]$Pass, [string]$Otp, [string]$Mode) {
    $b64 = { param($s) [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($s)) }
    switch ($Mode) {
        "None"   { return $Pass }
        "Append" { return "$Pass$Otp" }
        "Scrv1"  { return "SCRV1:$(& $b64 $Pass):$(& $b64 $Otp)" }
    }
}
if ((Format-VpnPassword "p" "1" "Scrv1") -ne "SCRV1:cA==:MQ==") { throw "Self-check failed: SCRV1 encoding" }

$running = Get-VpnProcess

if ($Status -or $Disconnect) {
    if (-not $running) { "${Name}: not connected."; return }

    if ($Disconnect) {
        $forced = -not (Stop-VpnGracefully)
        if ($forced) {
            # shortcut: the fallback force-kills the process and leaves its routes behind, they go away with the next graceful stop.
            # The process runs as the user (it was started through the service), so no elevation is involved.
            Stop-Process -Id $running.Id -Force -ErrorAction SilentlyContinue
            Start-Sleep -Seconds 1
            if (Get-VpnProcess) { throw "${Name}: openvpn.exe (PID $($running.Id)) could not be stopped. End it in Task Manager." }
        }
        Remove-Item $mgmtPw, $portFile, $pidFile -ErrorAction SilentlyContinue
        if ($forced) { "${Name}: disconnected (forced: the graceful stop was not available)." } else { "${Name}: disconnected." }
        return
    }

    $text = if (Test-Path $log) { "$(Get-Content $log -Raw)" } else { "" }
    $ip = [regex]::Matches($text, "ifconfig (\d+\.\d+\.\d+\.\d+)") | Select-Object -Last 1
    # CIM is used because the elevated process hides its start time from Get-Process in a normal shell.
    $started = (Get-CimInstance Win32_Process -Filter "ProcessId=$($running.Id)").CreationDate
    [pscustomobject]@{
        Name   = $Name
        # Only the part after the last restart counts: an older "Completed" says nothing about the tunnel now.
        State  = if (($text -split "process restarting")[-1] -match "Initialization Sequence Completed") { "Connected" } else { "Not established, see the log" }
        # The process can exit between the two lookups; then there is no start time.
        Uptime = if ($started) { "{0:d\.hh\:mm\:ss}" -f ((Get-Date) - $started) } else { "unknown" }
        VpnIp  = if ($ip) { $ip.Groups[1].Value } else { "unknown" }
        Log    = $log
    } | Format-List
    return
}

if ($running) { throw "${Name} is already running. Run with -Disconnect first." }

New-Item -ItemType Directory -Force $work | Out-Null
$fromOp = $PSCmdlet.ParameterSetName -eq "Connect"
# The user's own profile file is used in place; only the copy fetched from 1Password is temporary.
$cfg = if ($fromOp) { Join-Path $work "profile.ovpn" } else { (Resolve-Path $ProfilePath).Path }
$creds = Join-Path $work "creds.txt"
Remove-Item $creds -ErrorAction SilentlyContinue  # left behind if an earlier run was killed before its cleanup
$state = "timeout"

try {
    if ($fromOp) {
        $itemId = Resolve-OpItemId $Item
        $profileItemId = if ($ProfileItem -eq $Item) { $itemId } else { Resolve-OpItemId $ProfileItem }

        & op read "op://$Vault/$profileItemId/$ProfileFile" --out-file $cfg --force | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "op read failed for the profile $ProfileFile" }

        if ($OtpMode -eq "Auto") {
            $OtpMode = if (Select-String -Path $cfg -Pattern '^\s*static-challenge' -Quiet) { "Scrv1" } else { "Append" }
        }

        $user = Read-OpSecret "op://$Vault/$itemId/username"
        $pass = Read-OpSecret "op://$Vault/$itemId/password"
        # The code is fetched last so that it is as fresh as possible when the server checks it.
        $otp = if ($OtpMode -ne "None") { Read-OpSecret "op://$Vault/$itemId/${OtpField}?attribute=otp" } else { "" }
        $secret = Format-VpnPassword $pass $otp $OtpMode
        $authNote = "OTP mode was $OtpMode"
    }
    else {
        # Stdin instead of arguments: a command line is visible to every process of the user.
        [Console]::InputEncoding = [Text.UTF8Encoding]::new($false)
        $user = [Console]::In.ReadLine()
        $secret = [Console]::In.ReadLine()
        if (-not $user) { throw "${Name}: no user name was given on stdin." }
        # A separate variable: $OtpMode only accepts the values of its ValidateSet.
        $authNote = "the password was typed by the user"
    }

    [IO.File]::WriteAllText($creds, "$user`n$secret`n")
    Remove-Item $log -ErrorAction SilentlyContinue
    [IO.File]::WriteAllText($mgmtPw, [guid]::NewGuid().ToString("N") + "`n")

    # A free port picked by the OS, so that connections never fight over one.
    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
    $listener.Start()
    $port = $listener.LocalEndpoint.Port
    $listener.Stop()
    [IO.File]::WriteAllText($portFile, "$port")

    # shortcut: dynamic challenge (CRV1) prompts are not handled, answer them over the management interface if the server uses them.
    # --script-security 1 comes after --config and therefore wins: a profile cannot make OpenVPN run scripts.
    $options = "--config `"$cfg`" --script-security 1 --auth-user-pass `"$creds`" --log `"$log`" --management 127.0.0.1 $port `"$mgmtPw`""
    try {
        $vpnPid = Start-VpnViaService $options
    }
    catch {
        # Deliberately no elevated fallback: it would run OpenVPN as administrator on a profile the user can edit.
        throw "${Name}: the OpenVPN service did not start the connection ($_). Check that the service 'OpenVPNServiceInteractive' is running and that this account is an administrator or a member of the local group 'OpenVPN Administrators'."
    }
    [IO.File]::WriteAllText($pidFile, "$vpnPid")
    $proc = Get-Process -Id $vpnPid -ErrorAction SilentlyContinue

    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 500
        $text = if (Test-Path $log) { Get-Content $log -Raw } else { "" }
        if ($text -match "Initialization Sequence Completed") { $state = "connected"; break }
        if ($text -match "AUTH_FAILED") { $state = "auth failed ($authNote)"; break }
        if (-not $proc -or $proc.HasExited) { $state = "openvpn exited"; break }
    }
}
finally {
    Remove-Item $creds -ErrorAction SilentlyContinue
    if ($fromOp) { Remove-Item $cfg -ErrorAction SilentlyContinue }
}

if ($state -ne "connected") {
    Get-Content $log -Tail 20 -ErrorAction SilentlyContinue
    # Do not leave OpenVPN retrying in the background with a one-time code that is already spent.
    if (Get-VpnProcess) {
        if (-not (Stop-VpnGracefully)) { Get-VpnProcess | Stop-Process -Force -ErrorAction SilentlyContinue }
    }
    Remove-Item $mgmtPw, $portFile, $pidFile -ErrorAction SilentlyContinue
    throw "${Name} is not connected: $state. Full log: $log"
}
"${Name}: connected. Check with -Status, stop with -Disconnect."
