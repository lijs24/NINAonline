"""星枢 · NINA Web 控制站 —— FastAPI 入口。

职责:
- 按 provider 装配 SimGateway / LiveGateway,注入 app.state;
- 挂载 /api/* 路由 与 WebSocket /api/socket(事件推送);
- 提供前端页面(干净 URL)与静态资源。
局域网内任意设备访问 http://<工控机IP>:8788/ 即可。
"""
from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from api import router as api_router
from config import settings
from control import ControlLock
from events import bus

# 干净 URL -> 前端文件
PAGES: dict[str, str] = {
    "/": "nina-overview.html",
    "/overview": "nina-overview.html",
    "/equipment": "nina-equipment.html",
    "/camera": "nina-camera.html",
    "/mount": "nina-mount.html",
    "/focuser": "nina-focuser.html",
    "/guider": "nina-guider.html",
    "/filterwheel": "nina-filterwheel.html",
    "/sequence": "nina-sequence.html",
    "/framing": "nina-framing.html",
    "/aux": "nina-aux.html",
    "/library": "nina-library.html",
}


def build_gateway():
    if settings.provider == "live":
        from gateway.live import LiveGateway
        return LiveGateway(settings, bus)
    from gateway.sim.engine import SimGateway
    return SimGateway(settings, bus)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    gw = build_gateway()
    app.state.gateway = gw
    app.state.bus = bus
    app.state.lock = ControlLock(settings.control_lease_seconds)
    app.state.settings = settings
    await gw.start()
    bus.publish("SERVER-START", provider=gw.mode)
    try:
        yield
    finally:
        await gw.stop()


app = FastAPI(title="星枢 · NINA Web", version="1.0.0", lifespan=lifespan)
app.include_router(api_router)


@app.websocket("/api/socket")
async def socket(ws: WebSocket):
    await ws.accept()
    q = bus.subscribe()
    # 连接即补发最近 30 条历史,便于新页面快速对齐状态
    for evt in bus.history()[-30:]:
        with contextlib.suppress(Exception):
            await ws.send_json(evt.model_dump())
    try:
        while True:
            # 同时等"有新事件"与"客户端心跳",任一就绪即处理
            recv = asyncio.create_task(ws.receive_text())
            getev = asyncio.create_task(q.get())
            done, pending = await asyncio.wait(
                {recv, getev}, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            if getev in done:
                evt = getev.result()
                await ws.send_json(evt.model_dump())
            if recv in done:
                _ = recv.result()  # 客户端消息(ping/enable 等),此处忽略内容
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        bus.unsubscribe(q)


# ---- 前端页面(干净 URL) ---------------------------------------------------- #
def _page_response(filename: str) -> HTMLResponse:
    f = settings.frontend_dir / filename
    if f.is_file():
        return HTMLResponse(f.read_text(encoding="utf-8"))
    name = filename.replace("nina-", "").replace(".html", "")
    return HTMLResponse(_placeholder(name), status_code=200)


def _placeholder(name: str) -> str:
    return (f"<!doctype html><meta charset=utf-8>"
            f"<title>{name}</title>"
            f"<header id=app-topbar></header><script src='/nina-theme.js'></script>"
            f"<main style='padding:60px;text-align:center;color:#8b91a5;"
            f"font-family:serif'>「{name}」页面建设中 · 接口已就绪</main>")


for _path, _file in PAGES.items():
    def _make(fname: str):
        async def _route():
            return _page_response(fname)
        return _route
    app.add_api_route(_path, _make(_file), methods=["GET"], include_in_schema=False)


# ---- 静态资源(theme.js / fonts / assets) ---------------------------------- #
# 放在最后挂载,explicit 路由优先;未匹配的交给静态目录
app.mount("/", StaticFiles(directory=str(settings.frontend_dir), html=False), name="static")
