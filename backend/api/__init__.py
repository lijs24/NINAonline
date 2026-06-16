"""全部 /api/* 路由 —— 薄封装,逻辑都在 Gateway 里。

每域统一:GET /api/{domain} 读状态;POST /api/{domain}/action 执行动作。
写动作经协作锁 can_act 校验(被他人主控时 423)。
"""
from __future__ import annotations

import asyncio
from typing import Any, Optional

from fastapi import APIRouter, Body, Request, Response
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/api")


def _gw(request: Request):
    return request.app.state.gateway


def _bus(request: Request):
    return request.app.state.bus


def _lock(request: Request):
    return request.app.state.lock


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


async def _guard(request: Request, body: dict, domain: Optional[str] = None) -> Optional[dict]:
    """写动作前的校验:只读模式硬禁(可按域解禁) + 协作锁。
    返回 None=放行,否则返回错误响应体。domain 命中 settings.writable_domains 时,
    即便全局 readonly 也放行(仍需通过协作锁)。"""
    s = request.app.state.settings
    if s.readonly and (domain is None or domain not in s.writable_domains):
        return {"ok": False, "error": "只读监控模式 —— 已禁用对远程设备的所有操作",
                "readonly": True}
    sid = (body or {}).get("session_id", "")
    if not _lock(request).can_act(sid):
        return {"ok": False, "error": "其他会话正在主控,当前为监控模式,无法操作", "locked": True}
    return None


# --------------------------------------------------------------------------- #
# 总览 / 设备
# --------------------------------------------------------------------------- #
@router.get("/status")
async def status(request: Request):
    gw = _gw(request)
    s = request.app.state.settings
    summaries = await gw.device_summaries()
    connected = [d.type for d in summaries if d.connected]
    return {
        "ok": True,
        "provider": gw.mode,
        "readonly": s.readonly,
        "writable_domains": sorted(s.writable_domains),   # 只读下仍可控的域(按域解禁)
        "server_time": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "site": {"lat": s.site_lat, "lng": s.site_lng, "elev": s.site_elev},
        "connected_devices": connected,
        "device_count": len(summaries),
        "rig": "remote-rig-1",
    }


@router.get("/devices")
async def devices(request: Request):
    summaries = await _gw(request).device_summaries()
    return {"ok": True, "devices": [d.model_dump() for d in summaries],
            "default_device": "remote-rig-1"}


@router.get("/equipment/{device_type}/list")
async def equipment_list(request: Request, device_type: str):
    drivers = await _gw(request).list_drivers(device_type)
    return {"ok": True, "type": device_type, "drivers": [d.model_dump() for d in drivers]}


@router.post("/equipment/{device_type}/connect")
async def equipment_connect(request: Request, device_type: str, body: dict = Body(default={})):
    if (err := await _guard(request, body)):
        return JSONResponse(err, status_code=423)
    return await _gw(request).connect(device_type, body.get("driver_id"))


@router.post("/equipment/{device_type}/disconnect")
async def equipment_disconnect(request: Request, device_type: str, body: dict = Body(default={})):
    if (err := await _guard(request, body)):
        return JSONResponse(err, status_code=423)
    return await _gw(request).disconnect(device_type)


@router.get("/equipment")
async def equipment_overview(request: Request):
    """设备页聚合端点:后端一次性把"每类设备的连接状态 + 可用驱动列表"合并好,
    前端只需调这一个接口直接渲染,不必在浏览器里编排 11 次 list 调用。"""
    gw = _gw(request)
    summaries = await gw.device_summaries()
    drivers = await asyncio.gather(*[gw.list_drivers(s.type) for s in summaries])
    devices = []
    for s, drv in zip(summaries, drivers):
        d = s.model_dump()
        d["drivers"] = [x.model_dump() for x in drv]
        devices.append(d)
    return {"ok": True, "devices": devices}


# --------------------------------------------------------------------------- #
# 通用动作辅助
# --------------------------------------------------------------------------- #
async def _do_action(request: Request, fn_name: str, body: dict):
    domain = fn_name.replace("_action", "")          # camera_action → camera
    if (err := await _guard(request, body, domain)):
        return JSONResponse(err, status_code=423)
    action = (body or {}).get("action", "")
    params = (body or {}).get("params", {}) or {}
    fn = getattr(_gw(request), fn_name)
    return await fn(action, params)


def _state_route(domain: str, getter: str):
    @router.get(f"/{domain}", name=f"get_{domain}")
    async def _get(request: Request):
        st = await getattr(_gw(request), getter)()
        return st.model_dump()
    return _get


# 只读状态端点
_state_route("camera", "get_camera")
_state_route("mount", "get_mount")
_state_route("focuser", "get_focuser")
_state_route("filterwheel", "get_filterwheel")
_state_route("guider", "get_guider")
_state_route("rotator", "get_rotator")
_state_route("dome", "get_dome")
_state_route("flatdevice", "get_flatdevice")
_state_route("switch", "get_switch")
_state_route("weather", "get_weather")
_state_route("safety", "get_safety")
_state_route("sequence", "get_sequence")
_state_route("framing", "get_framing")


# --------------------------------------------------------------------------- #
# 各域动作
# --------------------------------------------------------------------------- #
@router.post("/camera/action")
async def camera_action(request: Request, body: dict = Body(default={})):
    return await _do_action(request, "camera_action", body)


@router.post("/mount/action")
async def mount_action(request: Request, body: dict = Body(default={})):
    return await _do_action(request, "mount_action", body)


@router.post("/focuser/action")
async def focuser_action(request: Request, body: dict = Body(default={})):
    return await _do_action(request, "focuser_action", body)


@router.post("/filterwheel/action")
async def filterwheel_action(request: Request, body: dict = Body(default={})):
    return await _do_action(request, "filterwheel_action", body)


@router.post("/guider/action")
async def guider_action(request: Request, body: dict = Body(default={})):
    return await _do_action(request, "guider_action", body)


@router.post("/rotator/action")
async def rotator_action(request: Request, body: dict = Body(default={})):
    return await _do_action(request, "rotator_action", body)


@router.post("/framing/action")
async def framing_action(request: Request, body: dict = Body(default={})):
    return await _do_action(request, "framing_action", body)


@router.post("/dome/action")
async def dome_action(request: Request, body: dict = Body(default={})):
    return await _do_action(request, "dome_action", body)


@router.post("/flatdevice/action")
async def flatdevice_action(request: Request, body: dict = Body(default={})):
    return await _do_action(request, "flatdevice_action", body)


@router.post("/switch/action")
async def switch_action(request: Request, body: dict = Body(default={})):
    return await _do_action(request, "switch_action", body)


@router.get("/conditions")
async def conditions(request: Request):
    return await _gw(request).get_conditions()


@router.post("/camera/platesolve")
async def camera_platesolve(request: Request, body: dict = Body(default={})):
    # 板解算属相机域:相机解禁则放行(仍需协作锁)
    if (err := await _guard(request, body, "camera")):
        return JSONResponse(err, status_code=423)
    res = await _gw(request).platesolve()
    return res.model_dump()


# --------------------------------------------------------------------------- #
# 相机影像
# --------------------------------------------------------------------------- #
@router.get("/camera/current-image")
async def current_image(request: Request):
    meta = await _gw(request).get_image_meta()
    return {"ok": True, "image": meta.model_dump() if meta else None}


@router.get("/camera/image")
async def camera_image(request: Request, image_id: Optional[int] = None):
    png = await _gw(request).get_image_png(image_id)
    if png is None:
        return JSONResponse({"ok": False, "error": "暂无图像"}, status_code=404)
    return Response(content=png, media_type="image/png",
                    headers={"Cache-Control": "no-store"})


@router.get("/camera/histogram")
async def camera_histogram(request: Request):
    meta = await _gw(request).get_image_meta()
    return {"ok": True, "histogram": meta.histogram if meta else []}


# --------------------------------------------------------------------------- #
# 自动对焦 / 导星曲线
# --------------------------------------------------------------------------- #
@router.get("/focuser/autofocus")
async def autofocus(request: Request):
    af = await _gw(request).get_autofocus()
    return af.model_dump()


@router.get("/guider/graph")
async def guider_graph(request: Request):
    steps = await _gw(request).get_guider_graph()
    return {"ok": True, "steps": [s.model_dump() for s in steps]}


# --------------------------------------------------------------------------- #
# 序列
# --------------------------------------------------------------------------- #
@router.post("/sequence/plan")
async def sequence_plan(request: Request, body: dict = Body(default={})):
    if (err := await _guard(request, body, "sequence")):   # 与 sequence/action 同域门禁
        return JSONResponse(err, status_code=423)
    return await _gw(request).sequence_action("set_plan", {"plan": body.get("plan", {})})


@router.post("/sequence/action")
async def sequence_action(request: Request, body: dict = Body(default={})):
    return await _do_action(request, "sequence_action", body)


# --------------------------------------------------------------------------- #
# 构图检索 / 图像库
# --------------------------------------------------------------------------- #
@router.get("/framing/search")
async def framing_search(request: Request, q: str = ""):
    targets = await _gw(request).framing_search(q)
    return {"ok": True, "targets": [t.model_dump() for t in targets]}


@router.get("/library/summary")
async def library_summary(request: Request):
    return await _gw(request).library_summary()


@router.get("/library/list")
async def library_list(request: Request, target: str = "", filter: str = "", limit: int = 200):
    items = await _gw(request).library_list(target=target or None,
                                            filter=filter or None, limit=limit)
    return {"ok": True, "items": [i.model_dump() for i in items]}


@router.get("/library/thumb")
async def library_thumb(request: Request, image_id: int):
    gw = _gw(request)
    fn = getattr(gw, "library_thumb", None)
    png = fn(image_id) if fn else None
    if png is None:
        return JSONResponse({"ok": False}, status_code=404)
    return Response(content=png, media_type="image/jpeg")


@router.get("/library/image")
async def library_image(request: Request, image_id: int):
    png = await _gw(request).get_image_png(image_id)
    if png is None:
        return JSONResponse({"ok": False}, status_code=404)
    return Response(content=png, media_type="image/png")


# --------------------------------------------------------------------------- #
# 协作锁
# --------------------------------------------------------------------------- #
@router.get("/control-role")
async def control_role_get(request: Request, session_id: str = ""):
    return _lock(request).state(session_id)


@router.post("/control-role")
async def control_role_post(request: Request, body: dict = Body(default={})):
    lock = _lock(request)
    return lock.claim(
        session_id=body.get("session_id", ""),
        label=body.get("session_label", "web"),
        client_ip=_client_ip(request),
        role=body.get("role", "monitor"))


# --------------------------------------------------------------------------- #
# 事件历史
# --------------------------------------------------------------------------- #
@router.get("/events")
async def events(request: Request, since: int = 0):
    evts = _bus(request).history(since)
    return {"ok": True, "events": [e.model_dump() for e in evts]}
