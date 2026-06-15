# 星枢 · NINA Web 控制站

一套部署在**远程台工控机**上的 Web 应用:让**同一局域网内任意设备**(手机/平板/笔记本浏览器)
都能像 NINA 桌面端一样控制整套天文摄影设备——设备连接、拍摄、赤道仪 GOTO、自动对焦、导星、
序列计划、图像库。基于已有的「星图册(Celestial Atlas)」前端风格,接口由 NINA 的能力面驱动设计。

**现状**:尚未接入真实硬件,内置一套**完整的模拟设备引擎**(`provider=sim`),开箱即可全流程演示;
接入真机时切到 `provider=live` 对接 NINA 的 [Advanced API 插件](https://github.com/christian-photo/ninaAPI)。

---

## 架构

```
 局域网内浏览器 (手机/平板/PC)
        │  HTTP + WebSocket
        ▼
 ┌─────────────────────────────────────────────┐
 │  星枢 后端 (FastAPI, 工控机)          │
 │   /api/* REST  +  /api/socket 事件流          │
 │   ┌─────────────── NinaGateway ────────────┐ │
 │   │  SimGateway(模拟引擎)  │ LiveGateway     │ │
 │   │  · 全设备状态机/物理     │ · httpx→NINA   │ │
 │   │  · 合成成像/自动对焦     │   /v2/api      │ │
 │   │  · 序列执行器            │ · ws 事件桥接  │ │
 │   └─────────────────────────┴────────────────┘ │
 └─────────────────────────────────────────────┘
        │ (provider=live 时)
        ▼
   NINA + Advanced API  ──►  真实设备 (ASCOM/Alpaca/原生)
```

- **前端**:纯静态(独立 HTML + `nina-theme.js` + 字体),无框架/无构建,离线可用。所有页面只认一套
  `/api/*` 契约,通过 `window.Ops` 客户端读写并订阅 WebSocket 事件。
- **后端**:`NinaGateway` 抽象屏蔽"设备世界",`SimGateway`/`LiveGateway` 两实现对前端透明。
- **协作锁**:局域网多人场景下,`/api/control-role` 提供"主控/监控"租约,同一时刻只有一个主控能下命令。

## 目录

```
nina-web/
├── backend/
│   ├── app.py              FastAPI 入口(装配网关/路由/WebSocket/静态)
│   ├── config.py           配置(环境变量覆盖)
│   ├── control.py          主控/监控协作锁
│   ├── events.py           事件总线 + 历史环形缓冲
│   ├── run.py              启动入口
│   ├── api/__init__.py     全部 /api/* 路由(薄封装)
│   └── gateway/
│       ├── base.py         NinaGateway 抽象接口
│       ├── models.py       领域模型(前端契约的单一事实来源)
│       ├── live.py         LiveGateway → NINA Advanced API
│       └── sim/            模拟引擎(engine/astro/imaging)
└── frontend/
    ├── nina-theme.js        星图册主题 + 顶栏 + 状态行 + Ops 客户端
    └── nina-*.html          10 个页面(见下)
```

## 页面(由 NINA 能力面推导)

| 路径 | 页面 | 功能 |
|---|---|---|
| `/overview` | 总览 | 设备汇总 · 当前目标 · 序列进度 · 事件流 · 最新影像 |
| `/equipment` | 设备 | 11 类设备的驱动选择/连接/断开/信息 |
| `/camera` | 相机 | 曝光/增益/制冷控制 · 单拍/循环 · 直方图 · 拉伸预览 |
| `/mount` | 赤道仪 | 坐标 · 目标检索 GOTO · 跟踪 · Park/Home · 子午翻转 · 手动微动 |
| `/focuser` | 对焦 | 位置/温度 · 温补 · 自动对焦 V 曲线 |
| `/guider` | 导星 | PHD2 式 RA/Dec 误差曲线 · RMS · 抖动 · 校准 |
| `/filterwheel` | 滤镜轮 | 滤镜切换 · 对焦偏置 |
| `/sequence` | 序列 | 计划编辑(目标/曝光行) · 高级触发(重复N轮/HFR拒帧/不安全中止/暮光起止) · 运行监视 |
| `/framing` | 构图 | 目标检索 · 视场框预览 · 旋转 · 转向居中 |
| `/aux` | 辅助 | 旋转器/圆顶/平场/开关 控制 + 天文台条件(日高度/暮光)/气象/安全 |
| `/library` | 图库 | 拍摄历史缩略图 · 元数据 · 按滤镜/目标统计 |

---

## 运行

### 安装(一次)
```bash
cd nina-web
python3 -m venv .venv           # 需 Python 3.9+
. .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r backend/requirements.txt
```

### 启动(模拟引擎,默认)
```bash
cd backend
python run.py
```
浏览器打开 `http://<工控机IP>:8788/`(局域网内其它设备同样用此地址)。
先到「设备」页点「一键连接全部」,即可在各页操作。

### 接入真实 NINA
1. 在工控机的 NINA 里安装 **Advanced API** 插件并启动(默认端口 1888)。
2. 以 live 模式启动本服务:
```bash
NINAWEB_PROVIDER=live NINAWEB_NINA_URL=http://127.0.0.1:1888 python run.py
```
> LiveGateway 已按 ninaAPI 的真实路由实现主要域(连接/相机/赤道仪/对焦/滤镜轮/导星/序列)及事件桥接;
> 真机首次接入时按 NINA 实际返回字段微调 `gateway/live.py` 的映射即可。

### 配置(环境变量)
| 变量 | 默认 | 说明 |
|---|---|---|
| `NINAWEB_PROVIDER` | `sim` | `sim` 模拟 / `live` 真机 |
| `NINAWEB_HOST` | `0.0.0.0` | 监听地址(0.0.0.0=局域网可达) |
| `NINAWEB_PORT` | `8788` | 端口 |
| `NINAWEB_NINA_URL` | `http://127.0.0.1:1888` | NINA Advanced API 地址 |
| `NINAWEB_LAT`/`NINAWEB_LNG` | `41.0`/`113.1` | 观测站点(坐标换算) |

---

## API 契约速览

统一信封:`{ ok, …, error? }`。每域 `GET /api/{domain}` 读状态,`POST /api/{domain}/action`
执行动作(body `{action, params, session_id}`,经协作锁校验)。

- 总览/设备:`/api/status` `/api/devices` `/api/equipment/{type}/{list|connect|disconnect}`
- 状态:`/api/{camera|mount|focuser|filterwheel|guider|rotator|dome|flatdevice|switch|weather|safety|sequence|framing}`
- 影像:`/api/camera/current-image` `/api/camera/image` `/api/camera/histogram`
- 对焦/导星:`/api/focuser/autofocus` `/api/guider/graph`
- 辅助:`/api/{dome,flatdevice,switch}/action` · `/api/conditions`(日高度/暮光/安全) · `/api/camera/platesolve`
- 序列:`/api/sequence/plan`(整份计划,含 loop_count/reject_hfr_over/abort_on_unsafe/暮光) `/api/sequence/action`
- 构图/图库:`/api/framing/search` `/api/library/{summary,list,thumb,image}`
- 协作:`/api/control-role`(GET 查 / POST 抢释)
- 事件:`/api/events`(历史回放) · `WS /api/socket`(实时,NINA 式事件名如 `IMAGE-SAVE`/`MOUNT-SLEWED`/`AUTOFOCUS-FINISHED`/`SEQUENCE-*`)

## 部署(工控机) —— `deploy/` 脚本
- **一键启动**:Windows 双击 `deploy\start.bat`(首次自动建环境装依赖);Linux/macOS `deploy/start.sh`。
- **开机自启服务**:管理员运行 `deploy\install-service.bat`(需 [nssm](https://nssm.cc)),注册为 Windows 服务并自动放行防火墙 8788。
- **冒烟自检**:`python deploy/smoke-test.py http://<IP>:8788` 跑通连接→拍摄→出图→转向→条件全链路(仅标准库)。
- 局域网访问:`http://<工控机局域网IP>:8788/`。
- 安全:当前**无鉴权**,假定运行在可信局域网(与 NINA Advanced API 一致)。如需暴露公网,务必加反代+鉴权。
