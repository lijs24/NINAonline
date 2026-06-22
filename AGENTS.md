# AGENTS.md — nina-web / 星枢(项目本体 · Codex / Claude 通用)

## 这是什么
**nina-web(代号「星枢」)= NINA Web 控制站**:FastAPI 后端 + 「星图册」风格前端,局域网/Tailscale 浏览器复现 NINA 桌面功能,控制 **130apo 远程天文台**(NINA + PHD2 + ASCOM GS Server 赤道仪)。
与 asiairbridge(ASIAIR 8794 那套)是两个独立项目。本仓只管 nina/130apo。

## 目录
- `backend/` —— FastAPI;`gateway/` 下 sim/live 双 provider;`gateway/designer.py`(序列设计器纯计算)、`gateway/nina_seq.py`(IR→NINA 高级序列 JSON 编译器)。
- `frontend/` —— `nina-*.html` + `nina-theme.{js,css}`(星图册风格:金色唯一强调色/无卡片/全站衬线/状态行不弹窗)。
- `deploy/`、`docs/`(设计文档 `docs/designer-plan.md`)、`state/`。
- 配套 NINA 源(主程序 + Advanced API 插件)在仓外的 `reference/`,接口对标用。

## 控制目标机 130apo
- Tailscale 节点 **astro-desktop-130apo / 100.107.140.109**(Windows)。进机器:**`ssh 130apo`**(User `win10`)。
- 服务:NINA Advanced API `http://…:1888/v2/api`(响应包 `.Response`,**版本 2.2.11.1**)、PHD2 `:4400`、nina-web live `:8788`(计划任务 `ninaweb` → `C:\Users\win10\nina-web\run-live-ro.bat`;重启 `schtasks /End /TN ninaweb` + `/Run /TN ninaweb`)。复杂/中文命令用 `powershell -EncodedCommand <base64(UTF-16LE)>`。

## 开发 / 验证
- 起 sim:`cd backend && NINAWEB_PROVIDER=sim ../.venv/bin/python run.py`(默认 8788)。
- 浏览器验证用 claude-in-chrome(用完关旧标签)。

## git
- 仓库 **`lijs24/NINAonline`**,分支 `main`。

## 安全(130apo 是真实在用天文台)
- 赤道仪移动(slew/goto/park/home/flip)有代码级护栏 `gateway/live.py:_slew_safety`;序列 `load/start` 受 `allow_sequence_start` 门控。
- 对真机默认**只读**;任何写操作(set_plan/load/start/move)先 sim 或空闲态验证。坑:ninaAPI **读** RA 返小时、**写**接口要度(×15);图像 raw 小端。

## Codex 协作
- 关键 codex 调用走**前台同步短任务**(共享运行时,长后台任务会被并发会话冲掉);codex 会画蛇添足/盲信,必须验收核实。

## 当前进度
序列设计器(`/designer`)**MVP-0~4 + 增强-5 NINA JSON 编译器**(`backend/gateway/nina_seq.py`,Codex 验证通过)已成。**下一步:增强-6 安全闸门** —— 生成的 .json 拿到 130apo NINA 桌面端手动 Load、肉眼确认无 Unknown 节点(尤其暗/平/偏置场与并行收尾这些真样本里没有、属源码推定的形态),通过后才开 load 下发 → start 监视。详见 `docs/designer-plan.md`。
