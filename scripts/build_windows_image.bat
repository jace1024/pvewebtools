@echo off
REM =====================================================================
REM  build_windows_image.bat  --  Windows 云镜像一键制作脚本
REM  在制作机（VM）内部以管理员身份运行。
REM
REM  两步式流程（最终镜像只保留内置 Administrator）：
REM     第一步 - 用任意管理员账号（例如 jace）登录并运行本脚本。
REM              它会关闭密码复杂度策略、启用内置 Administrator、
REM              设置其密码，然后退出，不做任何其他改动。
REM     第二步 - 注销，改用  Administrator  登录（密码 123456），
REM              再次运行本脚本。这一次它会：
REM                1) 静默安装 VirtIO 驱动 + QEMU Guest Agent
REM                2) 校验 QEMU-GA 服务是否运行
REM                3) 从本脚本尾部提取 unattend_fixed.xml 到 C:\
REM                4) 删除除 Administrator 之外的所有本地账号与配置文件
REM                5) 清理 Appx 残留（Edge 预置项必须保留，勿删）并烘入镜像调整项
REM                6) 执行 sysprep /generalize /oobe /shutdown /unattend:C:\unattend_fixed.xml
REM
REM  用法：
REM     * 必须以管理员身份运行。
REM     * 运行前请把 PVE 的 virtio-win-*.iso 挂到 VM 光驱（脚本会扫描 A:~Z:）。
REM     * 可选：主机名前缀（每台克隆 = 前缀 + 随机后缀，避免重名）：
REM           set COMPUTER_PREFIX=WIN
REM       也可以在提示时直接输入；留空则默认使用 WIN。
REM     * sysprep 结束后 VM 会自动关机 —— 不要再开机，
REM       直接导出 qcow2 或执行：qm template <vmid>
REM     * 如果 ISO 不在默认盘符，可手动指定：
REM           set VIRTIO_INSTALLER=X:\path\to\virtio-win-guest-tools.exe
REM =====================================================================
setlocal EnableExtensions

REM ---- force the Chinese code page so the messages below render correctly ----
chcp 936 >nul 2>&1

REM ---- 0) admin check ----
net session >nul 2>&1
if errorlevel 1 (
    echo [错误] 请以管理员身份运行本脚本。
    pause
    exit /b 1
)

echo ====================================================
echo  Windows 云镜像制作脚本（方式一：驱动 + sysprep）
echo ====================================================

REM ---- 0.6) two-step guard: the final (destructive) run must be the built-in Administrator ----
set "CUR_SID="
for /f "tokens=2 delims=," %%s in ('whoami /user /fo csv /nh') do set "CUR_SID=%%~s"
set "ISADMIN=0"
if "%CUR_SID:~-4%"=="-500" set "ISADMIN=1"
if "%ISADMIN%"=="1" goto :run_as_admin

echo.
echo ====================================================
echo  第 1 步 / 共 2 步 -- 准备内置 Administrator 账号
echo ====================================================
echo  当前账号 : %USERNAME%   (%CUR_SID%)
echo.
echo  当前不是内置 Administrator。最后一步必须在 Administrator
echo  账号下执行，才能把其他本地账号及其配置文件彻底删除。
echo.
echo  正在关闭密码复杂度策略并启用 Administrator ...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$f='C:\cp.inf'; Set-Content -Path $f -Value @('[Unicode]','Unicode=yes','[System Access]','PasswordComplexity = 0','MinimumPasswordLength = 0','PasswordHistorySize = 0','MaximumPasswordAge = -1') -Encoding Unicode; secedit /configure /db C:\cp.sdb /cfg $f /areas SECURITYPOLICY /quiet | Out-Null; Remove-Item 'C:\cp.inf','C:\cp.sdb' -Force -ErrorAction SilentlyContinue; net user Administrator /active:yes; net user Administrator 123456; $u=Get-LocalUser -Name Administrator; Write-Host ('[OK] Administrator 已启用 = ' + $u.Enabled + '   密码 = 123456')"
echo.
echo ====================================================
echo  接下来请这样做
echo ====================================================
echo    1) 注销当前账号   %USERNAME%
echo    2) 用   Administrator   登录   （密码：123456）
echo    3) 再次运行「同一个」脚本
echo.
echo  ^>^>  就是这一个文件，不需要找第二个脚本  ^<^<
echo  请直接用 Administrator 会话再运行一次
echo  build_windows_image.bat，它会自动接着执行第 2 步：
echo  删除其他账号 -^> 清理镜像 -^> sysprep -^> 自动关机。
echo.
echo  除此之外没有任何改动，你的数据完好无损。
echo ====================================================
echo.
pause
exit /b 0

:run_as_admin
echo ====================================================
echo  第 2 步 / 共 2 步 -- 以内置 Administrator 执行最终步骤
echo ====================================================
echo  当前账号 : %USERNAME%   (%CUR_SID%)
echo  本次将执行：删除其他所有本地账号与配置文件、
echo              清理镜像、然后执行 sysprep 并自动关机。
echo ====================================================
echo.

REM ---- 0.5) hostname prefix (optional, default WIN) ----
set "COMPUTER_PREFIX="
set /p COMPUTER_PREFIX=主机名前缀 [直接回车=WIN，例如 WIN]:
set "HPFX=WIN"
if defined COMPUTER_PREFIX set "HPFX=%COMPUTER_PREFIX%"
set "HPFX=%HPFX: =%"
echo [信息] 每台克隆的主机名 = %HPFX% + 随机后缀

echo.
echo [准备] 请确认 PVE 的 virtio-win-*.iso 已挂载到 VM 光驱。
echo        正在扫描所有盘符 (A-Z) 查找 virtio-win-guest-tools.exe ...
echo.

REM ---- 1) locate virtio-win-guest-tools.exe ----
set "INSTALLER="
if defined VIRTIO_INSTALLER (
    if exist "%VIRTIO_INSTALLER%" set "INSTALLER=%VIRTIO_INSTALLER%"
)
if not defined INSTALLER (
    for %%d in (A B D E F G H I J K L M N O P Q R S T U V W X Y Z) do (
        if exist "%%d:\virtio-win-guest-tools.exe" (
            set "INSTALLER=%%d:\virtio-win-guest-tools.exe"
        )
    )
)
if not defined INSTALLER (
    if exist "C:\virtio-win-guest-tools.exe" set "INSTALLER=C:\virtio-win-guest-tools.exe"
)

if not defined INSTALLER (
    echo [错误] 在所有盘符上都没找到 virtio-win-guest-tools.exe。
    echo.
    echo   本机当前可用盘符：
    for /f "skip=1" %%a in ('wmic logicaldisk get caption') do @if not "%%a"=="" echo(  %%a
    echo.
    echo   请检查：
    echo     1. 把 PVE 的 virtio-win-*.iso 挂到 VM 光驱。
    echo     2. 重新运行本脚本。
    echo   或手动指定：set VIRTIO_INSTALLER=X:\path\to\virtio-win-guest-tools.exe
    pause
    exit /b 1
)
echo [1/4] 已找到安装器: %INSTALLER%

REM ---- 2) install if QEMU-GA missing ----
sc query QEMU-GA >nul 2>&1
if errorlevel 1 (
    echo [2/4] 未检测到 QEMU-GA，正在静默安装驱动 + Guest Agent ...
    start "" /wait "%INSTALLER%" /S
    if errorlevel 1 (
        echo [错误] virtio-win-guest-tools 安装失败，请检查 ISO 是否完好。
        pause
        exit /b 1
    )
) else (
    echo [2/4] 已检测到 QEMU-GA，跳过安装。
)

REM ---- 3) wait for QEMU-GA RUNNING (up to ~120s) ----
echo [3/4] 正在等待 QEMU-GA 服务启动（最长 120 秒，下方会打点显示进度）...
set "GAOK=0"
for /L %%i in (1,1,24) do (
    sc query QEMU-GA | find "RUNNING" >nul 2>&1
    if not errorlevel 1 (
        set "GAOK=1"
        goto :gaready
    )
    <nul set /p=.
    timeout /t 5 >nul
)
echo.
:gaready
if "%GAOK%"=="0" (
    echo [警告] QEMU-GA 未在超时前进入 RUNNING 状态，仍将继续执行 sysprep。
    echo         如果部署后 Guest Agent 不可用，请在 PVE 侧开启 agent:1。
) else (
    echo [3/4] QEMU-GA 服务正在运行。
)

REM ---- 4) extract unattend_fixed.xml from this script tail, inject hostname prefix ----
echo [4/4] 正在提取 unattend_fixed.xml（仅含 oobeSystem 阶段）...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$t=Get-Content -Raw -Encoding UTF8 '%~f0'; $m=[regex]::Match($t,'(?s)===UNATTEND_DATA===\r?\n(.*?)\r?\n===END==='); if(-not $m.Success){ Write-Error '未找到 UNATTEND_DATA 数据块'; exit 1 }; $x=$m.Groups[1].Value.Trim(); try{ $d=New-Object System.Xml.XmlDocument; $d.LoadXml($x) }catch{ Write-Error ('生成的 unattend XML 格式非法: '+$_.Exception.Message); exit 1 }; [System.IO.File]::WriteAllText('C:\unattend_fixed.xml',$x,(New-Object System.Text.UTF8Encoding($false))); Write-Host '[OK] C:\unattend_fixed.xml 已写入，XML 校验通过，无 BOM'"
if errorlevel 1 (
    echo [错误] 写入 unattend_fixed.xml 失败。
    pause
    exit /b 1
)

REM ---- DRYRUN early exit: MUST happen before destructive cleanup ----
if defined DRYRUN (
    echo.
    echo [预演] 检测到 DRYRUN=1 -- 在任何破坏性清理或 sysprep 之前停止。
    echo [预演] unattend_fixed.xml 已写入 C:\unattend_fixed.xml。
    echo [预演] 确认无误后，去掉 DRYRUN 再运行一次即可真正执行。
    echo.
    pause
    exit /b 0
)

REM ---- 4.4) remove every local account + profile except the built-in Administrator ----
echo [账号] 正在删除除 Administrator 之外的所有本地账号与配置文件 ...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$me=(New-Object System.Security.Principal.NTAccount($env:USERNAME)).Translate([System.Security.Principal.SecurityIdentifier]).Value; if($me -notlike '*-500'){ Write-Host '[警告] 当前不是内置 Administrator -- 已跳过账号删除'; exit 0 }; $keep=@('-500','-501','-503','-504'); $prof=@(Get-CimInstance Win32_UserProfile | Where-Object { -not $_.Special -and $_.SID -ne $me }); foreach($p in $prof){ Write-Host ('  [配置文件] 正在删除 ' + $p.LocalPath); Remove-CimInstance -InputObject $p -ErrorAction SilentlyContinue }; $acc=@(Get-LocalUser | Where-Object { $keep -notcontains $_.SID.Value.Substring($_.SID.Value.LastIndexOf('-')) }); foreach($a in $acc){ Write-Host ('  [账号] 正在删除 ' + $a.Name); Remove-LocalUser -Name $a.Name -ErrorAction SilentlyContinue }; Write-Host '[OK] 账号清理完成'"
if errorlevel 1 (
    echo [警告] 账号删除过程中有报错，仍继续执行。
)

REM ---- 4.5) pre-sysprep cleanup: remove Appx/leftovers that block sysprep ----
echo [清理] 正在移除会阻塞 sysprep 的 Appx 残留（Edge 预置项保留不删）...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$sid=(New-Object System.Security.Principal.NTAccount($env:USERNAME)).Translate([System.Security.Principal.SecurityIdentifier]).Value; Remove-Item -Path 'HKCU:\Software\Microsoft\Windows Search\ProcessedSearchRoots' -Recurse -Force -ErrorAction SilentlyContinue; Remove-Item -Path \"HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Group Policy\$sid\" -Recurse -Force -ErrorAction SilentlyContinue; Remove-Item -Path \"HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Group Policy\State\$sid\" -Recurse -Force -ErrorAction SilentlyContinue; Remove-Item -Path \"HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Group Policy\Status\$sid\" -Recurse -Force -ErrorAction SilentlyContinue; Get-Service wuauserv,bits,dosvc,uhssvc -ErrorAction SilentlyContinue | Stop-Service -Force -ErrorAction SilentlyContinue; Stop-Process -Name msedge -Force -ErrorAction SilentlyContinue; $ep=@(Get-AppxProvisionedPackage -Online -ErrorAction SilentlyContinue | Where-Object {$_.PackageName -like 'Microsoft.MicrosoftEdge.Stable*'}); if($ep.Count -gt 0){ Write-Host '[OK] 映像中已有 Edge 预置项 -- 保持原样（sysprep 前删除预置项会导致 0x80073cf2）' } else { Write-Host '[警告] 映像中缺少 Edge 预置项：若某个用户已安装 Edge，sysprep 将以 0x80073cf2 失败' }; Get-AppxPackage -ErrorAction SilentlyContinue | Where-Object { $_.Name -ne 'Microsoft.MicrosoftEdge.Stable' -and $_.Name -ne 'Microsoft.MicrosoftEdge' } | Remove-AppxPackage -ErrorAction SilentlyContinue; if(Test-Path 'C:\Windows\Panther'){ Remove-Item -Path 'C:\Windows\Panther' -Recurse -Force -ErrorAction SilentlyContinue }; if(Test-Path 'C:\Windows\System32\Sysprep\Panther'){ Remove-Item -Path 'C:\Windows\System32\Sysprep\Panther' -Recurse -Force -ErrorAction SilentlyContinue }; Write-Host '[OK] 清理完成'"
if errorlevel 1 (
    echo [警告] 清理过程中有报错，仍继续执行。
)

REM ---- 4.55) bake image tweaks directly (replaces the old specialize pass) ----
echo [调整] 正在把密码策略 / RDP / 防火墙 / NRO / 遥测 等设置烘入镜像 ...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$f='C:\cp.inf'; Set-Content -Path $f -Value @('[Unicode]','Unicode=yes','[System Access]','PasswordComplexity = 0','MinimumPasswordLength = 0','PasswordHistorySize = 0','MaximumPasswordAge = -1') -Encoding Unicode; secedit /configure /db C:\cp.sdb /cfg $f /areas SECURITYPOLICY /quiet | Out-Null; Remove-Item 'C:\cp.inf','C:\cp.sdb' -Force -ErrorAction SilentlyContinue; reg add 'HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\OOBE' /v BypassNRO /t REG_DWORD /d 1 /f | Out-Null; reg add 'HKLM\SOFTWARE\Policies\Microsoft\Windows\DataCollection' /v AllowTelemetry /t REG_DWORD /d 0 /f | Out-Null; reg add 'HKLM\SOFTWARE\Policies\Microsoft\Windows\AdvertisingInfo' /v DisabledByGroupPolicy /t REG_DWORD /d 1 /f | Out-Null; Set-Service DiagTrack -StartupType Disabled -ErrorAction SilentlyContinue; reg add 'HKLM\SYSTEM\CurrentControlSet\Control\Terminal Server' /v fDenyTSConnections /t REG_DWORD /d 0 /f | Out-Null; netsh advfirewall set allprofiles state off | Out-Null; Write-Host '[OK] 调整项已烘入镜像（持久生效，无需 specialize 阶段）'"
if errorlevel 1 (
    echo [警告] 部分调整项设置失败，仍继续执行。
)

REM ---- 4.56) per-clone hostname hook: SetupComplete.cmd runs at end of OOBE, no answer file involved ----
echo [调整] 正在写入 SetupComplete.cmd : 主机名 = %HPFX% + 随机后缀 ...
if not exist "C:\Windows\Setup\Scripts" md "C:\Windows\Setup\Scripts" >nul 2>&1
> "C:\Windows\Setup\Scripts\SetupComplete.cmd" echo @echo off
>>"C:\Windows\Setup\Scripts\SetupComplete.cmd" echo powershell -NoProfile -ExecutionPolicy Bypass -Command "Rename-Computer -NewName ('%HPFX%'+(Get-Random -Maximum 99999)) -Force"
>>"C:\Windows\Setup\Scripts\SetupComplete.cmd" echo exit /b 0
echo [OK] C:\Windows\Setup\Scripts\SetupComplete.cmd 已写入

REM ---- 4.6) verify Edge provisioning state is self-consistent (mismatch => sysprep 0x80073cf2) ----
powershell -NoProfile -ExecutionPolicy Bypass -Command "$prov=@(Get-AppxProvisionedPackage -Online -ErrorAction SilentlyContinue | Where-Object {$_.PackageName -like 'Microsoft.MicrosoftEdge.Stable*'}); $usr=@(Get-AppxPackage -AllUsers -Name Microsoft.MicrosoftEdge.Stable -ErrorAction SilentlyContinue); if($prov.Count -gt 0){ Write-Host ('[OK] Edge 已预置到映像（预置项 '+$prov.Count+' 个 / 用户级安装 '+$usr.Count+' 个）-- sysprep 应用校验应可通过') } elseif($usr.Count -eq 0){ Write-Host '[OK] 映像中既无 Edge 预置项也无用户级安装 -- 状态自洽' } else { Write-Host ('[警告] Edge 存在用户级安装 (x'+$usr.Count+') 但没有预置项 -- sysprep 将报 0x80073cf2'); Write-Host '       修复：Get-AppxPackage -AllUsers -Name Microsoft.MicrosoftEdge.Stable | Remove-AppxPackage -AllUsers 后再删预置项，或恢复快照重跑本脚本' }"

REM ---- 5) run sysprep (auto shutdown) ----
echo.
echo ====================================================
echo  所有步骤已完成，即将执行：
echo    sysprep /generalize /oobe /shutdown /unattend:C:\unattend_fixed.xml
echo  VM 将会自动关机 -- 请不要再开机。
echo  关机后：导出 qcow2 或执行  qm template ^<vmid^>
echo ====================================================
echo.
%SystemRoot%\System32\Sysprep\sysprep.exe /generalize /oobe /shutdown /unattend:C:\unattend_fixed.xml
echo [完成] sysprep 已返回。如果 VM 没有自动关机，请检查 C:\unattend_fixed.xml。
pause
exit /b

===UNATTEND_DATA===
<?xml version="1.0" encoding="utf-8"?>
<unattend xmlns="urn:schemas-microsoft-com:unattend">

  <!-- NOTE: the specialize pass is intentionally REMOVED here.
       All image tweaks (password policy / RDP / firewall / NRO / telemetry) are
       baked into the image by build_windows_image.bat BEFORE sysprep, and the
       per-clone hostname is handled by C:\Windows\Setup\Scripts\SetupComplete.cmd.
       Keeping this answer file to a single pass avoids the
       "Windows could not parse or process ... pass [specialize]" failure. -->

  <!-- ============ oobeSystem: locale/keyboard + skip OOBE + fixed creds (no Cloudbase-Init) ============ -->
  <settings pass="oobeSystem">
    <component name="Microsoft-Windows-International-Core"
               processorArchitecture="amd64"
               publicKeyToken="31bf3856ad364e35"
               language="neutral"
               versionScope="nonSxS"
               xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State"
               xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
      <InputLocale>0804:00000804</InputLocale>
      <SystemLocale>zh-CN</SystemLocale>
      <UILanguage>zh-CN</UILanguage>
      <UserLocale>zh-CN</UserLocale>
    </component>

    <component name="Microsoft-Windows-Shell-Setup"
               processorArchitecture="amd64"
               publicKeyToken="31bf3856ad364e35"
               language="neutral"
               versionScope="nonSxS"
               xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State"
               xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
      <AutoLogon>
        <Enabled>true</Enabled>
        <Username>Administrator</Username>
        <Password>
          <Value>123456</Value>
          <PlainText>true</PlainText>
        </Password>
        <LogonCount>1</LogonCount>
      </AutoLogon>

      <OOBE>
        <HideEULAPage>true</HideEULAPage>
        <HideLocalAccountScreen>true</HideLocalAccountScreen>
        <HideOEMRegistrationScreen>true</HideOEMRegistrationScreen>
        <HideOnlineAccountScreens>true</HideOnlineAccountScreens>
        <HideWirelessSetupInOOBE>true</HideWirelessSetupInOOBE>
        <NetworkLocation>Work</NetworkLocation>
        <ProtectYourPC>3</ProtectYourPC>
      </OOBE>

      <TimeZone>China Standard Time</TimeZone>

      <!-- Enable Administrator with password 123456 (change for production; password complexity is disabled by the build script) -->
      <UserAccounts>
        <AdministratorPassword>
          <Value>123456</Value>
          <PlainText>true</PlainText>
        </AdministratorPassword>
      </UserAccounts>
    </component>
  </settings>

</unattend>
===END===
