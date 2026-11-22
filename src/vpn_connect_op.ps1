<#
.SYNOPSIS
Connects the OpenVPN community client using a profile, credentials and TOTP stored in 1Password.

.DESCRIPTION
Run from a NON-elevated shell: the 1Password CLI talks to the desktop app as the current user.
openvpn.exe is started through the OpenVPN interactive service, so it runs as the user and there is no
elevation prompt; nothing here ever runs elevated. Scripts named in a profile are blocked. The profile is
written to a temp folder for the start of the connection and removed as soon as it is up or has failed.
The user name and password never touch the disk: OpenVPN asks for them on its management interface
(--management-query-passwords) and this script answers. OpenVPN asks again whenever the server renegotiates
the keys or the connection restarts; the launcher then answers with a fresh code (-Reauth for 1Password).

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
    # The connection's internal key (its number, or its old name): used as a folder name.
    [Parameter(Mandatory = $true)][ValidatePattern('^[\w-]+\z')][string]$Name,
    [Parameter(Mandatory = $true, ParameterSetName = "Connect")][Parameter(Mandatory = $true, ParameterSetName = "Reauth")][string]$Vault,
    [Parameter(Mandatory = $true, ParameterSetName = "Connect")][Parameter(Mandatory = $true, ParameterSetName = "Reauth")][string]$Item,
    [Parameter(Mandatory = $true, ParameterSetName = "Connect")][string]$ProfileFile,
    [Parameter(ParameterSetName = "Connect")][string]$ProfileItem = $Item,
    [Parameter(ParameterSetName = "Connect")][Parameter(ParameterSetName = "Reauth")][string]$OtpField = "one-time password",
    # Auto: Scrv1 when the profile declares static-challenge, otherwise Append (password + code).
    [Parameter(ParameterSetName = "Connect")][Parameter(ParameterSetName = "Reauth")][ValidateSet("Auto", "Append", "Scrv1", "None")][string]$OtpMode = "Auto",
    # A running 1Password connection was asked for its credentials again: answer with a fresh code.
    [Parameter(Mandatory = $true, ParameterSetName = "Reauth")][switch]$Reauth,
    # Without 1Password: a profile file on disk; the user name and the password are read from stdin (two lines).
    [Parameter(Mandatory = $true, ParameterSetName = "File")][string]$ProfilePath,
    [Parameter(ParameterSetName = "Connect")][Parameter(ParameterSetName = "File")][int]$TimeoutSec = 90,
    [Parameter(Mandatory = $true, ParameterSetName = "Status")][switch]$Status,
    [Parameter(Mandatory = $true, ParameterSetName = "Disconnect")][switch]$Disconnect
)

$ErrorActionPreference = "Stop"

$work = Join-Path $env:TEMP "vpn_connect_op\$Name"
$log = Join-Path $work "openvpn.log"
$mgmtPw = Join-Path $work "mgmt.txt"    # random per-connection password of the management interface
$portFile = Join-Path $work "port.txt"  # management port on 127.0.0.1, used for a graceful stop
$pidFile = Join-Path $work "pid.txt"    # PID of the openvpn.exe of this connection
$modeFile = Join-Path $work "otpmode.txt"  # the OTP mode "Auto" resolved to, for -Reauth (which has no profile)

# Native commands (op) print UTF-8; without this a non-ASCII password or user name arrives garbled.
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)

# The 1Password CLI. Right after winget installed it, this process may still carry the PATH from before the
# installation, so the places winget puts its command links are looked at too.
function Find-OpCli {
    $found = Get-Command op -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($found) { return $found.Source }
    foreach ($candidate in "$env:LOCALAPPDATA\Microsoft\WinGet\Links\op.exe", "$env:ProgramFiles\WinGet\Links\op.exe") {
        if (Test-Path -LiteralPath $candidate) { return $candidate }
    }
    throw "The 1Password CLI (op) is not installed. Install it with: winget install AgileBits.1Password.CLI"
}

function Read-OpSecret([string]$Ref) {
    $value = & $script:op read $Ref
    if ($LASTEXITCODE -ne 0) { throw "op read failed for $Ref" }
    return $value
}

# Secret references reject characters such as ':' in item names, so items are addressed by ID.
function Resolve-OpItemId([string]$ItemName) {
    $json = & $script:op item get $ItemName --vault $Vault --format json
    if ($LASTEXITCODE -ne 0) { throw "op item get failed for $ItemName" }
    return ($json | ConvertFrom-Json).id
}

# The openvpn.exe of this connection, or nothing. A process is only ours when it has our PID, is an openvpn
# and started when pid.txt was written: after a reboot the number may belong to someone else's OpenVPN.
function Get-VpnProcess {
    $id = Get-Content $pidFile -TotalCount 1 -ErrorAction SilentlyContinue
    if (-not $id) { return }
    $process = Get-Process -Id $id -ErrorAction SilentlyContinue | Where-Object ProcessName -eq "openvpn"
    if (-not $process) { return }
    $written = (Get-Item -LiteralPath $pidFile).LastWriteTime
    $started = (Get-CimInstance Win32_Process -Filter "ProcessId=$id" -ErrorAction SilentlyContinue).CreationDate
    if ($started -and ($started -lt $written.AddSeconds(-120) -or $started -gt $written.AddSeconds(5))) { return }
    $process
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

# Stop-Process gets "Access is denied" on an openvpn.exe the service started; taskkill can end it.
function Stop-VpnForcibly([int]$Id) { taskkill /PID $Id /F 2>&1 | Out-Null }

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
        # The service normally answers at once; a hung one must not keep this script (and the card) waiting.
        $reading = $pipe.ReadAsync($buffer, 0, $buffer.Length)
        if (-not $reading.Wait(20000)) { throw "the OpenVPN service did not answer within 20 seconds" }
        $count = $reading.Result
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

# User name, then password (with the code as $Mode says) from 1Password. The code is read last, to be fresh.
function Get-OpCredentials([string]$ItemId, [string]$Mode) {
    $user = Read-OpSecret "op://$Vault/$ItemId/username"
    $pass = Read-OpSecret "op://$Vault/$ItemId/password"
    $otp = if ($Mode -ne "None") { Read-OpSecret "op://$Vault/$ItemId/${OtpField}?attribute=otp" } else { "" }
    return $user, (Format-VpnPassword $pass $otp $Mode)
}

# A management command argument: in double quotes, with backslashes and quotes escaped.
function ConvertTo-MgmtArgument([string]$Text) { '"' + $Text.Replace('\', '\\').Replace('"', '\"') + '"' }
if ((ConvertTo-MgmtArgument 'a"b\') -ne '"a\"b\\"') { throw "Self-check failed: management quoting" }

# Opens the management interface of this connection (retrying while OpenVPN starts) and logs in.
function Open-Mgmt([datetime]$Deadline) {
    while ($true) {
        try { $client = [Net.Sockets.TcpClient]::new("127.0.0.1", [int](Get-Content $portFile -TotalCount 1)); break }
        catch {
            if ((Get-Date) -gt $Deadline -or -not (Get-VpnProcess)) { throw "the OpenVPN management interface did not open" }
            Start-Sleep -Milliseconds 300
        }
    }
    $stream = $client.GetStream()
    $writer = [IO.StreamWriter]::new($stream)
    $writer.NewLine = "`n"
    $writer.AutoFlush = $true
    $writer.WriteLine((Get-Content $mgmtPw -TotalCount 1))
    @{ Client = $client; Reader = [IO.StreamReader]::new($stream); Writer = $writer; Pending = $null }
}

# The management lines that have arrived so far, without waiting for more.
function Receive-MgmtLines($Mgmt) {
    $lines = @()
    while ($true) {
        if (-not $Mgmt.Pending) { $Mgmt.Pending = $Mgmt.Reader.ReadLineAsync() }
        if (-not $Mgmt.Pending.IsCompleted) { return $lines }
        $line = $Mgmt.Pending.Result
        $Mgmt.Pending = $null
        if ($null -eq $line) { return $lines }
        $lines += $line
    }
}

# One command per write, each after the answer to the previous one. When both lines arrive together while
# OpenVPN waits for the password, it can process the first one twice and lose the second (seen on the Dev
# server: "username" logged twice, the password never arrived, the connect hung).
function Send-MgmtCredentials($Mgmt, [string]$User, [string]$Secret) {
    foreach ($command in "username `"Auth`" $(ConvertTo-MgmtArgument $User)", "password `"Auth`" $(ConvertTo-MgmtArgument $Secret)") {
        $Mgmt.Writer.WriteLine($command)
        $deadline = (Get-Date).AddSeconds(10)
        do {
            Start-Sleep -Milliseconds 100
            $answer = Receive-MgmtLines $Mgmt | Where-Object { $_ -match "^(SUCCESS: 'Auth'|ERROR)" } | Select-Object -First 1
        } until ($answer -or (Get-Date) -gt $deadline)
        $what = $command.Split(" ")[0]
        if (-not $answer) { throw "OpenVPN did not confirm the $what within 10 seconds" }
        if ($answer.StartsWith("ERROR")) { throw "OpenVPN refused the ${what}: $answer" }
    }
}

$passwordPrompt = ">PASSWORD:Need 'Auth'"

$running = Get-VpnProcess

if ($Status -or $Disconnect) {
    if (-not $running) { "${Name}: not connected."; return }

    if ($Disconnect) {
        $forced = -not (Stop-VpnGracefully)
        if ($forced) {
            # shortcut: the fallback force-kills the process and leaves its routes behind, they go away with the next graceful stop.
            # The process runs as the user (it was started through the service), so no elevation is involved.
            Stop-VpnForcibly $running.Id
            Start-Sleep -Seconds 1
            if (Get-VpnProcess) { throw "${Name}: openvpn.exe (PID $($running.Id)) could not be stopped. End it in Task Manager." }
        }
        Remove-Item $mgmtPw, $portFile, $pidFile, $modeFile -ErrorAction SilentlyContinue
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

if ($Reauth) {
    if (-not $running) { throw "${Name}: not connected." }
    $script:op = Find-OpCli
    $mode = if ($OtpMode -ne "Auto") { $OtpMode } else { "$(Get-Content $modeFile -TotalCount 1 -ErrorAction SilentlyContinue)" }
    if ($mode -notin "Append", "Scrv1", "None") { $mode = "Append" }
    # Credentials first: a 1Password approval may take a while, the management interface is held only briefly.
    $user, $secret = Get-OpCredentials (Resolve-OpItemId $Item) $mode
    $mgmt = Open-Mgmt (Get-Date).AddSeconds(10)
    try {
        $deadline = (Get-Date).AddSeconds(30)
        while ((Get-Date) -lt $deadline) {
            if (Receive-MgmtLines $mgmt | Where-Object { $_.StartsWith($passwordPrompt) }) {
                Send-MgmtCredentials $mgmt $user $secret
                "${Name}: the server asked to sign in again; a fresh code was sent."
                return
            }
            Start-Sleep -Milliseconds 200
        }
        throw "${Name}: OpenVPN did not ask for the credentials within 30 seconds."
    }
    finally { $mgmt.Client.Close() }
}

if ($running) { throw "${Name} is already running. Run with -Disconnect first." }

New-Item -ItemType Directory -Force $work | Out-Null
$fromOp = $PSCmdlet.ParameterSetName -eq "Connect"
# OpenVPN always gets a temporary copy in the work folder, fetched from 1Password or copied from the user's
# file. The service starts OpenVPN outside this process's environment, so it must not depend on a path that
# only this process can see (a packaged parent app, for example, gets a private view of %APPDATA%).
$cfg = Join-Path $work "profile.ovpn"
# Versions before 1.2.20 wrote the credentials to this file; one may be left behind by a killed run.
Remove-Item (Join-Path $work "creds.txt") -ErrorAction SilentlyContinue
$state = "timeout"
$mgmt = $null

try {
    if ($fromOp) {
        $script:op = Find-OpCli
        $itemId = Resolve-OpItemId $Item
        $profileItemId = if ($ProfileItem -eq $Item) { $itemId } else { Resolve-OpItemId $ProfileItem }

        & $script:op read "op://$Vault/$profileItemId/$ProfileFile" --out-file $cfg --force | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "op read failed for the profile $ProfileFile" }

        if ($OtpMode -eq "Auto") {
            $OtpMode = if (Select-String -Path $cfg -Pattern '^\s*static-challenge' -Quiet) { "Scrv1" } else { "Append" }
        }

        [IO.File]::WriteAllText($modeFile, $OtpMode)
        $user, $secret = Get-OpCredentials $itemId $OtpMode
        $authNote = "OTP mode was $OtpMode"
    }
    else {
        Copy-Item -LiteralPath $ProfilePath -Destination $cfg -Force
        # Stdin instead of arguments: a command line is visible to every process of the user.
        [Console]::InputEncoding = [Text.UTF8Encoding]::new($false)
        $user = [Console]::In.ReadLine()
        $secret = [Console]::In.ReadLine()
        if (-not $user) { throw "${Name}: no user name was given on stdin." }
        # A separate variable: $OtpMode only accepts the values of its ValidateSet.
        $authNote = "the password was typed by the user"
    }

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
    # --auth-user-pass without a file plus --management-query-passwords: OpenVPN asks for the credentials on the
    # management interface. --auth-nocache: it asks again at every key renegotiation and restart instead of
    # resending a spent one-time code. --reneg-sec 0 drops the client's own hourly renegotiation (the server's stays).
    $options = "--config `"$cfg`" --script-security 1 --reneg-sec 0 --auth-user-pass --auth-nocache --management-query-passwords --log `"$log`" --management 127.0.0.1 $port `"$mgmtPw`""
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
    $mgmt = Open-Mgmt $deadline
    $asked = 0
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 500
        # One answer per batch: a prompt repeated in the same batch is the same request.
        if (Receive-MgmtLines $mgmt | Where-Object { $_.StartsWith($passwordPrompt) }) {
            # A retry after a failed first attempt (a TLS timeout, say) asks again: 1Password gives a fresh code.
            if ($asked -and $fromOp) { $user, $secret = Get-OpCredentials $itemId $OtpMode }
            $asked++
            Send-MgmtCredentials $mgmt $user $secret
        }
        $text = if (Test-Path $log) { Get-Content $log -Raw } else { "" }
        if ($text -match "Initialization Sequence Completed") { $state = "connected"; break }
        if ($text -match "AUTH_FAILED") { $state = "auth failed ($authNote)"; break }
        if (-not $proc -or $proc.HasExited) { $state = "openvpn exited"; break }
    }
}
catch {
    # A failure after the start must still reach the cleanup below, or OpenVPN keeps waiting for a password.
    if (-not (Get-VpnProcess)) { throw }
    $state = "$_"
}
finally {
    # The management interface takes one client: free it for the launcher, which answers later requests.
    if ($mgmt) { $mgmt.Client.Close() }
    $user = $secret = $null
    Remove-Item $cfg -ErrorAction SilentlyContinue
}

if ($state -ne "connected") {
    Get-Content $log -Tail 20 -ErrorAction SilentlyContinue
    # Do not leave OpenVPN retrying in the background with a one-time code that is already spent.
    if (Get-VpnProcess) {
        if (-not (Stop-VpnGracefully)) { Get-VpnProcess | ForEach-Object { Stop-VpnForcibly $_.Id } }
    }
    Remove-Item $mgmtPw, $portFile, $pidFile, $modeFile -ErrorAction SilentlyContinue
    throw "${Name} is not connected: $state. Full log: $log"
}
"${Name}: connected. Check with -Status, stop with -Disconnect."
