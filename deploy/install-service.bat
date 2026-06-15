@echo off
REM 把星枢 NINA Web 注册为 Windows 服务(开机自启) —— 需要 nssm.exe
REM 1) 下载 nssm (https://nssm.cc), 把 nssm.exe 放到本目录或 PATH;
REM 2) 以"管理员"运行本脚本。
setlocal
cd /d "%~dp0\.."
set ROOT=%CD%

if not exist ".venv\Scripts\python.exe" (
  echo [安装] 首次需先建环境, 运行 deploy\start.bat 一次再来。
  pause & exit /b 1
)

set SVC=asiairbridge-ninaweb
nssm install %SVC% "%ROOT%\.venv\Scripts\python.exe" "%ROOT%\backend\run.py"
nssm set %SVC% AppDirectory "%ROOT%\backend"
nssm set %SVC% AppEnvironmentExtra NINAWEB_PROVIDER=sim NINAWEB_PORT=8788
nssm set %SVC% Start SERVICE_AUTO_START
nssm set %SVC% DisplayName "星枢 NINA Web 控制站"

REM 放行防火墙(局域网可达)
netsh advfirewall firewall add rule name="ninaweb-8788" dir=in action=allow protocol=TCP localport=8788 >nul 2>&1

nssm start %SVC%
echo.
echo 已注册并启动服务 %SVC% (开机自启), 端口 8788 已放行防火墙。
echo 局域网访问: http://(本机IP):8788/
echo 改真机: nssm set %SVC% AppEnvironmentExtra NINAWEB_PROVIDER=live NINAWEB_NINA_URL=http://127.0.0.1:1888
echo 卸载:   nssm remove %SVC% confirm
endlocal
