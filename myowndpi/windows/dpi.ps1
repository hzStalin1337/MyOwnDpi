# myowndpi kontrol betigi (Windows): .\dpi.ps1 start | stop | status | log | discord | roblox
param([string]$Command = "")

$ErrorActionPreference = "Stop"
$Dir     = Split-Path -Parent $MyInvocation.MyCommand.Path
$PidFile = Join-Path $Dir ".dpi.pid"
$LogFile = Join-Path $Dir "dpi.log"
$OutFile = Join-Path $Dir "dpi.out.log"
$PacUrl  = "http://127.0.0.1:8881/proxy.pac"
$Proxy   = "http://127.0.0.1:8881"
$InetKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings"
$RunKey  = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$RunName = "myowndpi"
$EnvVars = "HTTPS_PROXY", "HTTP_PROXY"

function Find-Python {
    # Arka planda pencere acmadan calismasi icin once pythonw
    foreach ($name in "pythonw.exe", "pyw.exe", "python.exe", "py.exe") {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd -and $cmd.Source -notlike "*\WindowsApps\*") { return $cmd.Source }
    }
    throw "Python bulunamadi. https://www.python.org/downloads/ adresinden kurun ('Add python.exe to PATH' isaretli olsun)."
}

function Test-Running {
    if (-not (Test-Path $PidFile)) { return $false }
    $p = Get-Process -Id ([int](Get-Content $PidFile)) -ErrorAction SilentlyContinue
    # Windows yeniden baslayinca ayni PID baska bir surece verilmis olabilir
    return [bool]($p -and $p.ProcessName -like "py*")
}

# WinINET'e (Edge, Discord/Electron vb.) proxy ayarinin degistigini bildir
function Update-InetSettings {
    $sig = '[DllImport("wininet.dll")] public static extern bool InternetSetOption(IntPtr h, int o, IntPtr b, int l);'
    if (-not ("MyOwnDpi.WinInet" -as [type])) {
        Add-Type -MemberDefinition $sig -Name WinInet -Namespace MyOwnDpi
    }
    $t = [MyOwnDpi.WinInet]
    [void]$t::InternetSetOption([IntPtr]::Zero, 39, [IntPtr]::Zero, 0)  # SETTINGS_CHANGED
    [void]$t::InternetSetOption([IntPtr]::Zero, 37, [IntPtr]::Zero, 0)  # REFRESH
}

# Discord'un guncelleyicisi PAC'i okumuyor, sadece HTTPS_PROXY'yi okuyor.
# Kullanici ortam degiskeni olarak ayarlaninca kendini yeniden baslatan ve
# kisayoldan acilan uygulamalar da gorur.
function Set-ProxyEnv {
    foreach ($v in $EnvVars) {
        [Environment]::SetEnvironmentVariable($v, $Proxy, "User")
        Set-Item "env:$v" $Proxy
    }
    [Environment]::SetEnvironmentVariable("NO_PROXY", "localhost,127.0.0.1,::1", "User")
    $env:NO_PROXY = "localhost,127.0.0.1,::1"
}

function Clear-ProxyEnv {
    foreach ($v in $EnvVars + "NO_PROXY") {
        [Environment]::SetEnvironmentVariable($v, $null, "User")
        Remove-Item "env:$v" -ErrorAction SilentlyContinue
    }
}

function Start-Dpi {
    if (Test-Running) { Write-Host "Zaten calisiyor (pid $(Get-Content $PidFile))."; return }
    $py = Find-Python
    $p = Start-Process -FilePath $py -ArgumentList "`"$Dir\dpi.py`"" -WindowStyle Hidden `
        -RedirectStandardError $LogFile -RedirectStandardOutput $OutFile -PassThru
    Set-Content $PidFile $p.Id
    Start-Sleep -Seconds 1
    if (-not (Test-Running)) { Write-Host "Baslatilamadi, log:"; Get-Content $LogFile; exit 1 }

    Set-ItemProperty $InetKey -Name AutoConfigURL -Value $PacUrl
    Update-InetSettings
    Write-Host "PAC ayarlandi: $PacUrl"
    Set-ProxyEnv
    # Ortam degiskeni ve PAC Windows yeniden baslayinca da kaliyor; proxy
    # kapali kalirsa bu uygulamalar baglanamaz. O yuzden oturum acilinca
    # otomatik baslat.
    $self = "powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$Dir\dpi.ps1`" start"
    Set-ItemProperty $RunKey -Name $RunName -Value $self
    Write-Host "myowndpi calisiyor. Sadece Discord ve Roblox tunelleniyor."
    Write-Host "Windows acilinca otomatik baslayacak (kapatmak icin: dpi.bat stop)."
    Write-Host "Discord/Roblox aciksa tamamen kapatip yeniden acin."
}

function Stop-Dpi {
    Remove-ItemProperty $InetKey -Name AutoConfigURL -ErrorAction SilentlyContinue
    Update-InetSettings
    Clear-ProxyEnv
    Remove-ItemProperty $RunKey -Name $RunName -ErrorAction SilentlyContinue
    if (Test-Running) { Stop-Process -Id ([int](Get-Content $PidFile)) -Force }
    Remove-Item $PidFile -ErrorAction SilentlyContinue
    Write-Host "myowndpi durduruldu, proxy ayarlari geri alindi."
}

function Show-Status {
    if (Test-Running) { Write-Host "Calisiyor (pid $(Get-Content $PidFile))" } else { Write-Host "Calismiyor" }
    $pac = (Get-ItemProperty $InetKey -Name AutoConfigURL -ErrorAction SilentlyContinue).AutoConfigURL
    Write-Host "PAC: $pac"
    Write-Host "HTTPS_PROXY (uygulamalar): $([Environment]::GetEnvironmentVariable('HTTPS_PROXY', 'User'))"
    $run = (Get-ItemProperty $RunKey -Name $RunName -ErrorAction SilentlyContinue).$RunName
    Write-Host "Otomatik baslatma: $(if ($run) { 'acik' } else { 'kapali' })"
}

function Restart-App([string]$which) {
    if (-not (Test-Running)) { Start-Dpi }
    Set-ProxyEnv  # bu oturumdan baslatilan uygulama da proxy'yi gorsun
    if ($which -eq "discord") {
        Get-Process Discord -ErrorAction SilentlyContinue | Stop-Process -Force
        Start-Sleep -Seconds 1
        $upd = Join-Path $env:LOCALAPPDATA "Discord\Update.exe"
        if (-not (Test-Path $upd)) { throw "Discord bulunamadi: $upd" }
        Start-Process $upd -ArgumentList "--processStart Discord.exe"
        Write-Host "Discord proxy ile baslatildi."
    } else {
        Get-Process RobloxPlayerBeta -ErrorAction SilentlyContinue | Stop-Process -Force
        Start-Sleep -Seconds 1
        $exe = Get-ChildItem (Join-Path $env:LOCALAPPDATA "Roblox\Versions") -Recurse `
                -Filter RobloxPlayerBeta.exe -ErrorAction SilentlyContinue |
               Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if (-not $exe) { throw "Roblox bulunamadi (%LOCALAPPDATA%\Roblox\Versions)." }
        Start-Process $exe.FullName
        Write-Host "Roblox proxy ile baslatildi."
    }
}

switch ($Command.ToLower()) {
    "start"   { Start-Dpi }
    "stop"    { Stop-Dpi }
    "status"  { Show-Status }
    "log"     { Get-Content $LogFile -Wait -Tail 30 }
    "discord" { Restart-App "discord" }
    "roblox"  { Restart-App "roblox" }
    default   { Write-Host "Kullanim: dpi.bat {start|stop|status|log|discord|roblox}"; exit 1 }
}
