# Lorakeet installer for Windows. The easy way: double-click "Install Lorakeet.cmd" in the Lorakeet folder.
#
# It checks for Python 3.11+ (and offers to install it), creates the virtual environment in .\venv, installs the
# requirements, then asks whether to add "Lorakeet" shortcuts (desktop and Start menu), to start Lorakeet at every
# sign-in, and to open it now. Safe to run again after updating: it only installs what changed. No admin rights.
#
#   powershell -ExecutionPolicy Bypass -File install.ps1               # asks as it goes
#   powershell -ExecutionPolicy Bypass -File install.ps1 -Yes          # no questions: the defaults (no autostart)
#   ... -Autostart / -NoAutostart / -NoShortcuts / -NoOpen / -NoPython  answer a question up front
#   ... -Uninstall                                                     # remove shortcuts, autostart and venv
#                                                                      # (asks before touching logged data)
param([switch]$Yes, [switch]$Autostart, [switch]$NoAutostart, [switch]$NoShortcuts, [switch]$NoOpen,
      [switch]$NoPython, [switch]$Uninstall, [switch]$Start, [string]$TaskName = "Lorakeet")
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
$Here = $PSScriptRoot
$PythonVersion = "3.12.10"  # what we offer to install when there's no Python 3.11+
$MinMinor, $MaxMinor = 11, 14  # Python 3.11 to 3.14: the meshtastic library doesn't support 3.15 yet
$venvPy = Join-Path $Here "venv\Scripts\python.exe"
$venvPyw = Join-Path $Here "venv\Scripts\pythonw.exe"
$shortcuts = @((Join-Path ([Environment]::GetFolderPath("Desktop")) "Lorakeet.lnk"),
               (Join-Path ([Environment]::GetFolderPath("Programs")) "Lorakeet.lnk"))

function Ask([string]$question, [bool]$default) {
    # No console to answer from (CI, -Yes): take the default.
    if ($Yes -or [Console]::IsInputRedirected -or -not [Environment]::UserInteractive) { return $default }
    $a = Read-Host "$question $(if ($default) { '[Y/n]' } else { '[y/N]' })"
    if ([string]::IsNullOrWhiteSpace($a)) { return $default }
    return $a.Trim().ToLower().StartsWith("y")
}

function Invoke-Quiet([scriptblock]$cmd) {
    # Windows PowerShell 5.1 turns any redirected stderr line of a native program into a terminating error while
    # $ErrorActionPreference is "Stop" (a pip or winget warning would end the install): run those with "Continue".
    $old = $ErrorActionPreference; $ErrorActionPreference = "Continue"
    try { & $cmd } finally { $ErrorActionPreference = $old }
}

function Python-Info($exe, $pre) {
    # "3.12|C:\...\python.exe", or $null if it doesn't run
    try { $out = Invoke-Quiet { & $exe @pre -c "import sys; print('%d.%d|%s' % (sys.version_info[0], sys.version_info[1], sys.executable))" 2>$null } }
    catch { return $null }
    if ($LASTEXITCODE -ne 0 -or -not $out) { return $null }
    $v, $path = ($out | Select-Object -Last 1).Trim().Split("|", 2)
    return @{ Version = $v; Minor = [int]$v.Split(".")[1]; Major = [int]$v.Split(".")[0]; Path = $path }
}

function Find-Python {
    # The py launcher asked for each supported version (plain "py -3" picks the newest, which may be too new), then
    # python on PATH, then python.org's own folders (a fresh install isn't on this window's PATH yet). The Microsoft
    # Store's Python is skipped: it redirects writes under AppData into a private folder of its own.
    $candidates = @()
    foreach ($m in $MaxMinor..$MinMinor) { $candidates += , @("py", "-3.$m") }
    $candidates += @(@("python"), @("python3"))
    Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe", "$env:ProgramFiles\Python3*\python.exe" `
        -ErrorAction SilentlyContinue | Sort-Object FullName -Descending | ForEach-Object { $candidates += , @($_.FullName) }
    foreach ($c in $candidates) {
        $exe = $c[0]; $pre = @($c | Select-Object -Skip 1)
        if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
        $i = Python-Info $exe $pre
        if (-not $i) { continue }
        if ($i.Path -match "\\WindowsApps\\") { Write-Host "Skipping the Microsoft Store's Python ($($i.Version)): Lorakeet needs a python.org Python."; continue }
        if ($i.Major -eq 3 -and $i.Minor -ge $MinMinor -and $i.Minor -le $MaxMinor) { return @{ Exe = $exe; Pre = $pre; Version = $i.Version } }
        Write-Host "Found Python $($i.Version) at $exe, but Lorakeet needs 3.$MinMinor to 3.$MaxMinor."
    }
    return $null
}

function Install-Python {
    # Windows' package manager when it's there (it checks the installer's signature), else python.org's own
    # installer. Just for this user: no admin prompt.
    # --source winget: on a fresh Windows the Microsoft Store source isn't ready yet ("server certificate did not
    # match"), and with two sources answering winget refuses to pick one. Its output is shown only if it fails.
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Write-Host "Installing Python 3.12 with winget (a minute or two)..."
        $out = Invoke-Quiet { winget install --id Python.Python.3.12 --exact --source winget --scope user --silent `
            --accept-package-agreements --accept-source-agreements 2>&1 }
        if ($LASTEXITCODE -eq 0) { return }
        $out | Select-Object -Last 6 | ForEach-Object { Write-Host "  $_" }
        Write-Host "winget didn't manage it; trying python.org's installer instead."
    }
    $arch = if ($env:PROCESSOR_ARCHITECTURE -eq "ARM64") { "arm64" } else { "amd64" }
    $url = "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-$arch.exe"
    $file = Join-Path $env:TEMP "python-$PythonVersion-$arch.exe"
    Write-Host "Downloading $url ..."
    $ProgressPreference = "SilentlyContinue"
    Invoke-WebRequest $url -OutFile $file
    $sig = Get-AuthenticodeSignature $file
    if ($sig.Status -ne "Valid" -or $sig.SignerCertificate.Subject -notmatch "Python Software Foundation") {
        Remove-Item $file -Force
        throw "The downloaded Python installer isn't signed by the Python Software Foundation; not running it."
    }
    Write-Host "Installing Python $PythonVersion (about a minute)..."
    Start-Process $file -ArgumentList "/quiet InstallAllUsers=0 PrependPath=1 Include_test=0" -Wait
    Remove-Item $file -Force -ErrorAction SilentlyContinue
}

function Lorakeet-Processes {
    # Only this install's own Python processes: run from its venv, or running one of its three scripts by full path.
    # (Not anything whose command line merely mentions the folder, or a sibling folder like "lorakeet-copy".)
    $venv = (Join-Path $Here "venv") + "\"
    $scripts = "server.py", "supervise.pyw", "lorakeet.pyw" | ForEach-Object { Join-Path $Here $_ }
    Get-CimInstance Win32_Process -Filter "Name like 'python%'" | Where-Object {
        $exe, $cmd = $_.ExecutablePath, $_.CommandLine
        ($exe -and $exe.StartsWith($venv, [StringComparison]::OrdinalIgnoreCase)) -or
        ($cmd -and ($scripts | Where-Object { $cmd.IndexOf($_, [StringComparison]::OrdinalIgnoreCase) -ge 0 }))
    }
}

# ------------------------------------------------------------------ uninstall
if ($Uninstall) {
    Write-Host "Uninstalling Lorakeet from $Here"
    $dataDir = $null
    if (Test-Path $venvPy) {
        try { $dataDir = (Invoke-Quiet { & $venvPy -c "from config import CFG; print(CFG['storage']['data_dir'])" 2>$null } | Select-Object -Last 1) } catch { }
    }
    $running = @(Lorakeet-Processes)
    if ($running) { $running | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; Write-Host "Stopped Lorakeet." }
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($task -and ($task.Actions | Where-Object { $_.WorkingDirectory -eq $Here })) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed the autostart task."
    }
    $sh = New-Object -ComObject WScript.Shell
    foreach ($p in $shortcuts) {
        if ((Test-Path $p) -and ($sh.CreateShortcut($p).WorkingDirectory -eq $Here)) { Remove-Item $p -Force; Write-Host "Removed $p" }
    }
    if (Test-Path (Join-Path $Here "venv")) { Remove-Item (Join-Path $Here "venv") -Recurse -Force; Write-Host "Removed the Python environment (venv)." }
    # Logged data is never removed by default, and never without typing the word.
    if ($dataDir -and (Test-Path $dataDir) -and -not $Yes -and (Ask "Also delete everything Lorakeet logged ($dataDir)? This can't be undone." $false)) {
        if ((Read-Host "Type DELETE to confirm") -ceq "DELETE") {
            # Only Lorakeet's own files: the data folder may be one the user also keeps other things in.
            $own = "mesh.db*", "debug.db*", "server.log*", "supervise.log*", "login.json", "stations.json", "peers.json",
                   "install_id", "storage_state.json", "*.tmp", "backup-tmp", "demo", "radio-backups"
            foreach ($pat in $own) { Get-ChildItem -LiteralPath $dataDir -Filter $pat -Force -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force }
            if (-not (Get-ChildItem -LiteralPath $dataDir -Force)) { Remove-Item -LiteralPath $dataDir -Force; Write-Host "Deleted $dataDir" }
            else { Write-Host "Deleted Lorakeet's files from $dataDir (other files there were left alone)." }
        } else { Write-Host "Kept $dataDir" }
    } elseif ($dataDir -and (Test-Path $dataDir)) {
        Write-Host "Your logged data is still in $dataDir"
    }
    Write-Host ""
    Write-Host "Done. Your settings (lorakeet.toml, if any) and the program files are in $Here : delete that folder to finish."
    exit 0
}

# ------------------------------------------------------------------ install / update
if (Test-Path $venvPy) {
    # A venv copied from another computer (OneDrive), or whose Python was uninstalled or is out of range, never works
    # again: start it afresh rather than failing on every run.
    $i = Python-Info $venvPy @()
    if (-not $i -or $i.Minor -lt $MinMinor -or $i.Minor -gt $MaxMinor) {
        Write-Host "The Python environment in venv doesn't work on this computer$(if ($i) { " (Python $($i.Version))" }); making a new one."
        Remove-Item (Join-Path $Here "venv") -Recurse -Force
    }
}
if (-not (Test-Path $venvPy)) {
    $py = Find-Python
    if (-not $py -and -not $NoPython -and (Ask "Lorakeet needs Python 3.11 to 3.14, and this computer doesn't have it. Install Python $PythonVersion now?" $true)) {
        Install-Python
        $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
        $py = Find-Python
    }
    if (-not $py) {
        Write-Host "Python 3.11 to 3.14 is needed (not the Microsoft Store's). Install it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH'), then run this again."
        exit 1
    }
    Write-Host "Creating the Python environment for Lorakeet (Python $($py.Version))..."
    & $py.Exe @($py.Pre) -m venv venv
    if ($LASTEXITCODE -ne 0) { throw "Couldn't create the virtual environment." }
}
Write-Host "Installing what Lorakeet needs (a minute the first time)..."
& $venvPy -m pip install --disable-pip-version-check --prefer-binary -q -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "pip couldn't install the requirements (see above)." }
$version = & $venvPy server.py --version
if ($LASTEXITCODE -ne 0) { throw "Lorakeet didn't start (see above)." }
Write-Host "$version is installed."
Write-Host ""

function Points-Elsewhere([string]$dir) { return $dir -and ($dir.TrimEnd("\") -ne $Here.TrimEnd("\")) }

$sh = New-Object -ComObject WScript.Shell
$other = $shortcuts | Where-Object { (Test-Path $_) -and (Points-Elsewhere $sh.CreateShortcut($_).WorkingDirectory) } | Select-Object -First 1
$makeShortcuts = -not $NoShortcuts -and (Ask "Add a Lorakeet shortcut to the desktop and the Start menu?" $true)
if ($makeShortcuts -and $other -and -not (Ask "The Lorakeet shortcuts open another copy, in $($sh.CreateShortcut($other).WorkingDirectory). Point them at this one instead?" $false)) {
    $makeShortcuts = $false
    Write-Host "Kept the shortcuts pointing at the other copy."
}
if ($makeShortcuts) {
    foreach ($p in $shortcuts) {
        $s = $sh.CreateShortcut($p)
        $s.TargetPath = $venvPyw
        $s.Arguments = '"' + (Join-Path $Here "lorakeet.pyw") + '"'
        $s.WorkingDirectory = $Here
        $s.IconLocation = (Join-Path $Here "static\favicon.ico") + ",0"
        $s.Description = "Open Lorakeet (starts it if it isn't running)"
        $s.Save()
    }
    Write-Host "Added the Lorakeet shortcuts: double-click one to open Lorakeet (it starts it if needed)."
}

$auto = $Autostart -or (-not $NoAutostart -and (Ask "Start Lorakeet automatically when you sign in, so it's always logging?" $false))
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
$elsewhere = $existing.Actions | Where-Object { Points-Elsewhere $_.WorkingDirectory } | Select-Object -First 1
if ($auto -and $elsewhere -and -not (Ask "Another copy of Lorakeet ($($elsewhere.WorkingDirectory)) already starts at sign-in. Start this one instead?" $false)) {
    $auto = $false
    Write-Host "Kept the other copy starting at sign-in."
}
if ($auto) {
    $action = New-ScheduledTaskAction -Execute $venvPyw -Argument "supervise.pyw" -WorkingDirectory $Here
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
    $trigger.Delay = "PT30S"  # let the USB radio and network come up first
    $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
        -Description "Lorakeet Meshtastic logger (supervise.pyw keeps server.py running)" -Force | Out-Null
    Write-Host "Lorakeet will start at every sign-in (Task Scheduler task '$TaskName')."
    if ($Start) { Start-ScheduledTask -TaskName $TaskName; Write-Host "Started." }
}

Write-Host ""
if (-not $NoOpen -and (Ask "Open Lorakeet now?" $true)) {
    Start-Process -FilePath $venvPyw -ArgumentList ('"' + (Join-Path $Here "lorakeet.pyw") + '"') -WorkingDirectory $Here
    Write-Host "Opening Lorakeet in your browser (it takes a few seconds to start). The first time, a setup page asks a few questions."
} else {
    $hasShortcut = $shortcuts | Where-Object { (Test-Path $_) -and -not (Points-Elsewhere $sh.CreateShortcut($_).WorkingDirectory) }
    Write-Host "To open Lorakeet: $(if ($hasShortcut) { 'the Lorakeet shortcut, or ' })venv\Scripts\pythonw lorakeet.pyw"
}
Write-Host "Optional: let other devices on your network log in with  venv\Scripts\python server.py --set-password"
Write-Host "To uninstall: double-click 'Uninstall Lorakeet.cmd'."
