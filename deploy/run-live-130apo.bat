@echo off
REM ============================================================================
REM 星枢 NINA Web —— 130apo 现场启动器(live + 完全可控 + 赤道仪 slew 护栏)
REM ----------------------------------------------------------------------------
REM 部署:130apo(DESKTOP-QTBRTL3,Tailscale astro-desktop-130apo)由 Windows
REM       计划任务 "ninaweb" 拉起本脚本;改动后重启:
REM         schtasks /End /TN ninaweb  &  schtasks /Run /TN ninaweb
REM 现场盒子上历史上叫 run-live-ro.bat(置于 nina-web 根),本文件是其版本化形态。
REM 局域网/Tailscale 访问:http://<本机IP>:8788/
REM ============================================================================
setlocal
cd /d "%~dp0\.."

set NINAWEB_PROVIDER=live
REM 完全可控(关只读);设备级写权限全开,赤道仪由下方代码级护栏把关
set NINAWEB_READONLY=0

REM ── 赤道仪 slew 安全护栏(防打腿,实现见 backend/gateway/live.py 的 _slew_safety)──
REM 目标地平高度 < 此值(度)直接拒绝转向;0 = 仅拒地平线以下
set NINAWEB_MOUNT_MIN_ALT_DEG=0
REM 过中天限位(度):>0 时,目标在当前墩侧越过中天进入配重上扬区超过该角度即拒绝;
REM 0 = 关闭(依赖 ASCOM SideOfPier 约定,需用一次已知指向核对方向再开;
REM     在此之前由 NINA 自身的中天翻转作权威保护)
set NINAWEB_MOUNT_MERIDIAN_LIMIT_DEG=0

REM 本机 NINA Advanced API(ninaAPI 插件)
set NINAWEB_NINA_URL=http://127.0.0.1:1888
set NINAWEB_PORT=8788

if not exist "state" mkdir "state"
cd backend
"..\.venv\Scripts\python.exe" run.py > "..\state\out.log" 2>&1
endlocal
