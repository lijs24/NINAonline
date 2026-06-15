@echo off
REM 星枢 NINA Web —— Windows 启动脚本(工控机用)
REM 首次运行自动建虚拟环境并装依赖;之后直接启动。
setlocal
cd /d "%~dp0\.."

if not exist ".venv\Scripts\python.exe" (
  echo [安装] 创建虚拟环境...
  py -3 -m venv .venv || python -m venv .venv
  ".venv\Scripts\python.exe" -m pip install --upgrade pip
  ".venv\Scripts\python.exe" -m pip install -r backend\requirements.txt
)

REM 可在此切换真机: set NINAWEB_PROVIDER=live & set NINAWEB_NINA_URL=http://127.0.0.1:1888
if "%NINAWEB_PROVIDER%"=="" set NINAWEB_PROVIDER=sim
if "%NINAWEB_PORT%"=="" set NINAWEB_PORT=8788

echo [启动] provider=%NINAWEB_PROVIDER%  端口=%NINAWEB_PORT%
echo        局域网访问: http://<本机IP>:%NINAWEB_PORT%/
cd backend
"..\.venv\Scripts\python.exe" run.py
endlocal
