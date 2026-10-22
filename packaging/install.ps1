<#
    Install (or update) ATI_Rig on this machine.

    Copies the built folder to %USERPROFILE%\ATI_Rig and creates Start-menu
    and Desktop shortcuts carrying the app's own icon.

    Why the user profile and not C:\ or Program Files:
      * managed machines (many university machines do this) block executables
        run from the root of C:;
      * Program Files is read-only for normal users, and this app writes
        sequences\, output\, config\ and logs\ beside its .exe.

    Why the profile root and not %LOCALAPPDATA%: those folders are reached by
    hand - sequences\ and output\ are the operator's own files.
    AppData is hidden by default, so the install was invisible to the person
    who has to open it.

    Run from the project root, after packaging\build.bat:

        powershell -ExecutionPolicy Bypass -File packaging\install.ps1

    Re-running it upgrades in place: the program files are replaced, and
    config\, sequences\, output\ and logs\ are left alone.

    -Dest <path>     install somewhere else
    -NoDesktop       Start menu only
    -RefreshIcons    clear Windows' icon cache and restart Explorer. Only
                     needed if the shortcuts still show a previous version's
                     picture; the install already nudges the shell without it.
    -Seed            on a FRESH install only, copy the project's sequences\*.txt
                     into the installed copy, so it is
                     usable immediately. Never touches an existing install's
                     data. Only meaningful when installing from a checkout that
                     already has them.
#>
[CmdletBinding()]
param(
    [string]$Dest = (Join-Path $env:USERPROFILE 'ATI_Rig'),
    [switch]$NoDesktop,
    [switch]$Seed,
    [switch]$RefreshIcons
)

$ErrorActionPreference = 'Stop'

$root  = Split-Path -Parent $PSScriptRoot
$build = Join-Path $root 'dist\ATI_Rig'
$exeName = 'ATI_Rig.exe'

if (-not (Test-Path (Join-Path $build $exeName))) {
    throw "No build found at $build`nRun packaging\build.bat first."
}

# Data the operator has accumulated; never overwritten by an upgrade.
$keep = @('config', 'sequences', 'output', 'logs')

$upgrade = Test-Path (Join-Path $Dest $exeName)
Write-Host ("{0} {1}" -f $(if ($upgrade) { 'Upgrading' } else { 'Installing' }), $Dest)

# Renamed ATI_App -> ATI_Rig (Oct 2026). A first install beside an old
# %USERPROFILE%\ATI_App takes its config\, sequences\ and output\ along
# (copied, not moved: the old folder is left for the operator to delete), and
# the old shortcuts are removed so there is one way to start the program.
$old = Join-Path $env:USERPROFILE 'ATI_App'
if (-not $upgrade -and (Test-Path (Join-Path $old 'ATI_App.exe'))) {
    New-Item -ItemType Directory -Path $Dest -Force | Out-Null
    foreach ($d in 'config', 'sequences', 'output') {
        $src = Join-Path $old $d
        if (Test-Path $src) {
            Copy-Item $src -Destination $Dest -Recurse -Force
            Write-Host "  carried over from ATI_App: $d\"
        }
    }
}
foreach ($lnk in @((Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\ATI_App.lnk'),
                   (Join-Path ([Environment]::GetFolderPath('Desktop')) 'ATI_App.lnk'))) {
    if (Test-Path $lnk) { Remove-Item $lnk -Force; Write-Host "  removed old shortcut: $lnk" }
}

if ($upgrade) {
    # Replace the program, leave the data. _internal is fully rebuilt so a
    # module dropped from the spec does not linger.
    Remove-Item (Join-Path $Dest '_internal') -Recurse -Force -ErrorAction SilentlyContinue
    Get-ChildItem $Dest -File | Remove-Item -Force
} else {
    New-Item -ItemType Directory -Path $Dest -Force | Out-Null
}

Get-ChildItem $build | Where-Object { $keep -notcontains $_.Name } | ForEach-Object {
    Copy-Item $_.FullName -Destination $Dest -Recurse -Force
}

$exe = Join-Path $Dest $exeName

# Shortcuts point at this rather than at the .exe's embedded resource. Windows
# caches icons per source path, so an .exe whose picture changed but whose path
# did not keeps serving the stale bitmap; a file we control lets an upgrade
# swap it, and -RefreshIcons is the hammer for when the cache is stubborn.
$icoSrc = Join-Path $PSScriptRoot 'app_icon.ico'
$ico = Join-Path $Dest 'app_icon.ico'
if (Test-Path $icoSrc) { Copy-Item $icoSrc -Destination $ico -Force } else { $ico = "$exe,0" }

if ($Seed -and -not $upgrade) {
    # The sequences are the operator's own protocols. Seeding them makes a
    # fresh install usable without hunting for the checkout.
    $seq = Join-Path $root 'sequences'
    if (Test-Path $seq) {
        New-Item -ItemType Directory -Path (Join-Path $Dest 'sequences') -Force | Out-Null
        Copy-Item (Join-Path $seq '*.txt') -Destination (Join-Path $Dest 'sequences') -Force
        $n = (Get-ChildItem (Join-Path $Dest 'sequences') -Filter *.txt).Count
        Write-Host "  seeded: sequences\ ($n files)"
    }
}

function New-Shortcut([string]$LinkPath, [string]$Target, [string]$Description, [string]$IconRef) {
    $shell = New-Object -ComObject WScript.Shell
    $sc = $shell.CreateShortcut($LinkPath)
    $sc.TargetPath       = $Target
    $sc.WorkingDirectory = Split-Path -Parent $Target
    $sc.IconLocation     = $IconRef
    $sc.Description      = $Description
    $sc.Save()
    Write-Host "  shortcut: $LinkPath"
}

$startMenu = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
$iconRef = $(if ($ico -like '*,0') { $ico } else { "$ico,0" })

New-Shortcut (Join-Path $startMenu 'ATI Rig.lnk') $exe `
             'ATI Rig - 3-axis rig + ATI F/T sensor' $iconRef

if (-not $NoDesktop) {
    New-Shortcut (Join-Path ([Environment]::GetFolderPath('Desktop')) 'ATI Rig.lnk') $exe `
                 'ATI Rig - 3-axis rig + ATI F/T sensor' $iconRef
}

# Tell the shell the icons moved. Cheap and non-disruptive; usually enough.
if (-not ([System.Management.Automation.PSTypeName]'AtiShell.Api').Type) {
    Add-Type -Namespace AtiShell -Name Api -MemberDefinition @'
[DllImport("shell32.dll", CharSet=CharSet.Auto, SetLastError=true)]
public static extern void SHChangeNotify(int wEventId, uint uFlags, IntPtr dwItem1, IntPtr dwItem2);
'@
}
[AtiShell.Api]::SHChangeNotify(0x08000000, 0x0000, [IntPtr]::Zero, [IntPtr]::Zero)  # SHCNE_ASSOCCHANGED
& ie4uinit.exe -show 2>$null

if ($RefreshIcons) {
    Write-Host '  clearing the icon cache and restarting Explorer...'
    Stop-Process -Name explorer -Force -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 800
    Remove-Item (Join-Path $env:LOCALAPPDATA 'Microsoft\Windows\Explorer\iconcache*.db') `
                -Force -ErrorAction SilentlyContinue
    if (-not (Get-Process explorer -ErrorAction SilentlyContinue)) { Start-Process explorer.exe }
}

Write-Host ''
Write-Host "Done. $exe"
