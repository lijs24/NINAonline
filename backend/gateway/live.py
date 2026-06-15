"""LiveGateway —— 对接真实 NINA(Advanced API 插件 ninaAPI)。

把本系统的 NinaGateway 接口翻译成 NINA 的 REST(/v2/api)调用,并把 NINA 的
WebSocket(/v2/socket)事件桥接到本地 EventBus。

说明:本网关按 reference/ninaAPI 的真实路由与响应信封实现,但由于开发期
没有真机/真 NINA 可测,部分域是"尽力而为"映射;未覆盖处返回断开默认值并
记录,不会让前端崩溃。真机接入后按 NINA 实际返回字段微调映射即可。
provider=sim 是开发主路径,provider=live 用于现场对接。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

import httpx

from config import Settings
from events import EventBus
from gateway import models as m
from gateway.base import NinaGateway

# 本系统设备名 -> NINA Advanced API 路径段
NINA_DEV = {
    "camera": "camera", "mount": "mount", "focuser": "focuser",
    "filterwheel": "filterwheel", "guider": "guider", "rotator": "rotator",
    "dome": "dome", "flatdevice": "flatdevice", "switch": "switch",
    "weather": "weather", "safetymonitor": "safetymonitor",
}


class LiveGateway(NinaGateway):
    mode = "live"

    def __init__(self, settings: Settings, bus: EventBus) -> None:
        self.s = settings
        self.bus = bus
        self._api = settings.nina_base_url.rstrip("/") + settings.nina_api_path
        # 连接超时压到 2s:NINA 不可达时快速失败,不拖垮整页
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=2.0))
        self._ws_task: asyncio.Task | None = None
        self._stop = False

    async def start(self) -> None:
        self._ws_task = asyncio.create_task(self._ws_bridge())

    async def stop(self) -> None:
        self._stop = True
        if self._ws_task:
            self._ws_task.cancel()
        await self._client.aclose()

    # -- 底层 ------------------------------------------------------------- #
    async def _get(self, path: str, **params) -> Any:
        """调用 NINA REST,解开 {Response,Success,Error} 信封。"""
        try:
            r = await self._client.get(self._api + path, params=params)
            data = r.json()
            if isinstance(data, dict) and "Success" in data:
                if not data.get("Success", False):
                    return {"_error": data.get("Error", "NINA 调用失败")}
                return data.get("Response")
            return data
        except Exception as e:
            return {"_error": str(e)}

    # -- 设备生命周期 ----------------------------------------------------- #
    async def list_drivers(self, device_type: str) -> list[m.DriverDescriptor]:
        dev = NINA_DEV.get(device_type, device_type)
        resp = await self._get(f"/equipment/{dev}/list-devices")
        out = []
        if isinstance(resp, list):
            for d in resp:
                out.append(m.DriverDescriptor(
                    id=str(d.get("Id", d.get("id", ""))),
                    name=str(d.get("Name", d.get("DisplayName", "设备"))),
                    category=str(d.get("Category", "ASCOM"))))
        return out

    async def connect(self, device_type: str, driver_id: str | None) -> dict:
        dev = NINA_DEV.get(device_type, device_type)
        params = {"to": driver_id} if driver_id else {}
        resp = await self._get(f"/equipment/{dev}/connect", **params)
        if isinstance(resp, dict) and resp.get("_error"):
            return {"ok": False, "error": resp["_error"]}
        return {"ok": True}

    async def disconnect(self, device_type: str) -> dict:
        dev = NINA_DEV.get(device_type, device_type)
        resp = await self._get(f"/equipment/{dev}/disconnect")
        if isinstance(resp, dict) and resp.get("_error"):
            return {"ok": False, "error": resp["_error"]}
        return {"ok": True}

    async def device_summaries(self) -> list[m.DeviceSummary]:
        # 11 个设备 info 并行拉取:NINA 在线时几乎瞬时,离线时也只等一个连接超时
        async def one(t: str) -> m.DeviceSummary:
            info = await self._get(f"/equipment/{NINA_DEV.get(t, t)}/info")
            ok = isinstance(info, dict) and not info.get("_error")
            conn = bool(info.get("Connected")) if ok else False
            name = info.get("Name", "") if ok else ""
            return m.DeviceSummary(type=t, connected=conn, name=name,
                                   state="idle" if conn else "disconnected")
        return list(await asyncio.gather(*[one(t) for t in m.ALL_DEVICE_TYPES]))

    # -- 相机 ------------------------------------------------------------- #
    async def get_camera(self) -> m.CameraState:
        info = await self._get("/equipment/camera/info")
        c = m.CameraState()
        if isinstance(info, dict) and not info.get("_error"):
            c.connected = bool(info.get("Connected"))
            c.name = info.get("Name", "")
            c.temperature = float(info.get("Temperature") or 0)
            c.target_temperature = float(info.get("TemperatureSetPoint") or 0)
            c.cooler_on = bool(info.get("CoolerOn"))
            c.cooler_power = float(info.get("CoolerPower") or 0)
            c.gain = int(info.get("Gain") or 0)
            c.offset = int(info.get("Offset") or 0)
            c.chip_width = int(info.get("XSize") or 0)
            c.chip_height = int(info.get("YSize") or 0)
            c.pixel_size_um = float(info.get("PixelSize") or 0)
            c.has_cooler = bool(info.get("CanSetTemperature"))
            c.is_exposing = bool(info.get("IsExposing"))
            c.state = "exposing" if c.is_exposing else "idle"
        return c

    async def camera_action(self, action: str, p: dict) -> dict:
        if action == "capture":
            return _ok(await self._get("/equipment/camera/capture",
                                       duration=p.get("exposure"), gain=p.get("gain")))
        if action == "abort":
            return _ok(await self._get("/equipment/camera/abort-exposure"))
        if action == "cool":
            return _ok(await self._get("/equipment/camera/cool",
                                       temperature=p.get("temperature"), cancel="false"))
        if action == "warm":
            return _ok(await self._get("/equipment/camera/warm", cancel="false"))
        if action == "set_control":
            # NINA 无通用 set;按项分发(此处仅示意常见项)
            return {"ok": True, "note": "live 模式部分控制项需经 NINA profile"}
        return {"ok": False, "error": f"live 未映射相机动作 {action}"}

    async def get_image_meta(self, image_id=None) -> m.ImageMeta | None:
        info = await self._get("/image-history", all="true")
        if isinstance(info, list) and info:
            last = info[-1]
            return m.ImageMeta(
                image_id=len(info) - 1, width=0, height=0,
                exposure_s=float(last.get("ExposureTime") or 0),
                gain=int(last.get("Gain") or 0), offset=int(last.get("Offset") or 0),
                bin=1, filter=last.get("Filter", ""), hfr=float(last.get("HFR") or 0),
                stars=int(last.get("Stars") or 0), captured_at=last.get("Date", ""),
                image_url="/api/camera/image")
        return None

    async def get_image_png(self, image_id=None, stretch=True) -> bytes | None:
        try:
            idx = image_id if image_id is not None else 0
            r = await self._client.get(self._api + f"/image/{idx}",
                                       params={"resize": "true", "autoPrepare": "true"})
            data = r.json()
            b64 = data.get("Response") if isinstance(data, dict) else None
            if b64:
                import base64
                return base64.b64decode(b64)
        except Exception:
            pass
        return None

    # -- 赤道仪 ----------------------------------------------------------- #
    async def get_mount(self) -> m.MountState:
        info = await self._get("/equipment/mount/info")
        mo = m.MountState(site_lat=self.s.site_lat, site_lng=self.s.site_lng)
        if isinstance(info, dict) and not info.get("_error"):
            mo.connected = bool(info.get("Connected"))
            mo.name = info.get("Name", "")
            mo.ra_hours = float(info.get("RightAscension") or 0)
            mo.dec_degrees = float(info.get("Declination") or 0)
            mo.ra_text = info.get("RightAscensionString", "")
            mo.dec_text = info.get("DeclinationString", "")
            mo.tracking = bool(info.get("TrackingEnabled"))
            mo.slewing = bool(info.get("Slewing"))
            mo.at_park = bool(info.get("AtPark"))
            mo.side_of_pier = str(info.get("SideOfPier", "unknown")).lower()
            mo.altitude = float(info.get("Altitude") or 0)
            mo.azimuth = float(info.get("Azimuth") or 0)
        return mo

    async def mount_action(self, action: str, p: dict) -> dict:
        if action == "slew":
            return _ok(await self._get("/equipment/mount/slew",
                                       ra=p.get("ra"), dec=p.get("dec")))
        if action == "park":
            return _ok(await self._get("/equipment/mount/park"))
        if action == "unpark":
            return _ok(await self._get("/equipment/mount/unpark"))
        if action == "home":
            return _ok(await self._get("/equipment/mount/home"))
        if action == "flip":
            return _ok(await self._get("/equipment/mount/flip"))
        if action == "stop":
            return _ok(await self._get("/equipment/mount/slew/stop"))
        if action == "set_tracking":
            return _ok(await self._get("/equipment/mount/tracking",
                                       enabled=str(bool(p.get("on", True))).lower()))
        if action == "sync":
            return _ok(await self._get("/equipment/mount/sync", ra=p.get("ra"), dec=p.get("dec")))
        return {"ok": False, "error": f"live 未映射赤道仪动作 {action}"}

    # -- 调焦 ------------------------------------------------------------- #
    async def get_focuser(self) -> m.FocuserState:
        info = await self._get("/equipment/focuser/info")
        f = m.FocuserState()
        if isinstance(info, dict) and not info.get("_error"):
            f.connected = bool(info.get("Connected"))
            f.name = info.get("Name", "")
            f.position = int(info.get("Position") or 0)
            f.temperature = float(info.get("Temperature") or 0)
            f.is_moving = bool(info.get("IsMoving"))
        return f

    async def get_autofocus(self) -> m.AutoFocusResult:
        info = await self._get("/equipment/focuser/last-af")
        af = m.AutoFocusResult()
        if isinstance(info, dict) and not info.get("_error"):
            for pt in info.get("MeasurePoints", []) or []:
                af.points.append(m.AutoFocusPoint(
                    position=int(pt.get("Position", 0)), hfr=float(pt.get("Value", 0))))
            cm = info.get("CalculatedFocusPoint") or {}
            af.best_position = int(cm.get("Position", 0)) if cm else None
        return af

    async def focuser_action(self, action: str, p: dict) -> dict:
        if action in ("move_abs", "move_rel"):
            pos = p.get("position")
            return _ok(await self._get("/equipment/focuser/move", position=pos))
        if action == "halt":
            return _ok(await self._get("/equipment/focuser/stop-move"))
        if action == "autofocus_start":
            return _ok(await self._get("/equipment/focuser/auto-focus"))
        return {"ok": False, "error": f"live 未映射调焦动作 {action}"}

    # -- 滤镜轮 ----------------------------------------------------------- #
    async def get_filterwheel(self) -> m.FilterWheelState:
        info = await self._get("/equipment/filterwheel/info")
        fw = m.FilterWheelState()
        if isinstance(info, dict) and not info.get("_error"):
            fw.connected = bool(info.get("Connected"))
            fw.name = info.get("Name", "")
            sel = info.get("SelectedFilter") or {}
            fw.position = int(sel.get("Id", 0)) if sel else 0
            for fl in info.get("AvailableFilters", []) or []:
                fw.filters.append(m.FilterSlot(
                    position=int(fl.get("Id", 0)), name=fl.get("Name", "")))
        return fw

    async def filterwheel_action(self, action: str, p: dict) -> dict:
        if action == "change":
            return _ok(await self._get("/equipment/filterwheel/change-filter",
                                       filterId=p.get("position")))
        return {"ok": False, "error": f"live 未映射滤镜轮动作 {action}"}

    # -- 导星 ------------------------------------------------------------- #
    async def get_guider(self) -> m.GuiderState:
        info = await self._get("/equipment/guider/info")
        g = m.GuiderState()
        if isinstance(info, dict) and not info.get("_error"):
            g.connected = bool(info.get("Connected"))
            g.name = info.get("Name", "")
            g.state = str(info.get("State", "idle")).lower()
            g.pixel_scale = float(info.get("PixelScale") or 1)
            rms = info.get("RMSError") or {}
            if rms:
                g.rms_ra = float((rms.get("RA") or {}).get("Arcseconds") or 0)
                g.rms_dec = float((rms.get("Dec") or {}).get("Arcseconds") or 0)
                g.rms_total = float((rms.get("Total") or {}).get("Arcseconds") or 0)
        return g

    async def get_guider_graph(self) -> list[m.GuideStep]:
        info = await self._get("/equipment/guider/graph")
        steps = []
        if isinstance(info, dict):
            for i, pt in enumerate(info.get("GuideSteps", []) or []):
                steps.append(m.GuideStep(
                    t=float(i), ra_raw=float(pt.get("RADistanceRaw") or 0),
                    dec_raw=float(pt.get("DECDistanceRaw") or 0),
                    ra_dist=float(pt.get("RADistanceRaw") or 0),
                    dec_dist=float(pt.get("DECDistanceRaw") or 0)))
        return steps

    async def guider_action(self, action: str, p: dict) -> dict:
        if action == "start":
            return _ok(await self._get("/equipment/guider/start"))
        if action == "stop":
            return _ok(await self._get("/equipment/guider/stop"))
        if action == "clear_calibration":
            return _ok(await self._get("/equipment/guider/clear-calibration"))
        return {"ok": False, "error": f"live 未映射导星动作 {action}"}

    # -- 其余设备:尽力读 info,动作多数未在 NINA API 暴露 ----------------- #
    async def _simple_info(self, dev: str) -> dict:
        info = await self._get(f"/equipment/{dev}/info")
        return info if isinstance(info, dict) and not info.get("_error") else {}

    async def get_rotator(self) -> m.RotatorState:
        i = await self._simple_info("rotator")
        return m.RotatorState(connected=bool(i.get("Connected")), name=i.get("Name", ""),
                              position=float(i.get("Position") or 0))

    async def get_dome(self) -> m.DomeState:
        i = await self._simple_info("dome")
        return m.DomeState(connected=bool(i.get("Connected")), name=i.get("Name", ""),
                           azimuth=float(i.get("Azimuth") or 0))

    async def get_flatdevice(self) -> m.FlatDeviceState:
        i = await self._simple_info("flatdevice")
        return m.FlatDeviceState(connected=bool(i.get("Connected")), name=i.get("Name", ""),
                                 brightness=int(i.get("Brightness") or 0))

    async def get_switch(self) -> m.SwitchState:
        i = await self._simple_info("switch")
        return m.SwitchState(connected=bool(i.get("Connected")), name=i.get("Name", ""))

    async def get_weather(self) -> m.WeatherState:
        i = await self._simple_info("weather")
        return m.WeatherState(connected=bool(i.get("Connected")), name=i.get("Name", ""),
                              temperature=float(i.get("Temperature") or 0),
                              humidity=float(i.get("Humidity") or 0),
                              cloud_cover=float(i.get("CloudCover") or 0))

    async def get_safety(self) -> m.SafetyState:
        i = await self._simple_info("safetymonitor")
        return m.SafetyState(connected=bool(i.get("Connected")), name=i.get("Name", ""),
                             is_safe=bool(i.get("IsSafe", True)))

    async def rotator_action(self, action: str, p: dict) -> dict:
        if action == "move":
            return _ok(await self._get("/equipment/rotator/move", position=p.get("position")))
        return {"ok": False, "error": "live 未映射"}

    async def dome_action(self, action: str, p: dict) -> dict:
        mp = {"open_shutter": "/equipment/dome/open", "close_shutter": "/equipment/dome/close",
              "park": "/equipment/dome/park", "find_home": "/equipment/dome/home",
              "stop": "/equipment/dome/stop"}
        if action in mp:
            return _ok(await self._get(mp[action]))
        if action == "slew":
            return _ok(await self._get("/equipment/dome/slew", azimuth=p.get("azimuth")))
        if action == "set_follow":
            return _ok(await self._get("/equipment/dome/set-follow",
                                       enabled=str(bool(p.get("on", False))).lower()))
        return {"ok": False, "error": "live 未映射圆顶动作"}

    async def flatdevice_action(self, action: str, p: dict) -> dict:
        if action == "set_light":
            return _ok(await self._get("/equipment/flatdevice/set-light",
                                       on=str(bool(p.get("on", False))).lower()))
        if action == "set_brightness":
            return _ok(await self._get("/equipment/flatdevice/set-brightness",
                                       brightness=p.get("brightness")))
        if action in ("open_cover", "close_cover"):
            return _ok(await self._get("/equipment/flatdevice/set-cover",
                                       closed=str(action == "close_cover").lower()))
        return {"ok": False, "error": "live 未映射平场动作"}

    async def switch_action(self, action: str, p: dict) -> dict:
        if action == "set":
            return _ok(await self._get("/equipment/switch/set",
                                       index=p.get("id"), value=p.get("value")))
        return {"ok": False, "error": "live 未映射开关动作"}

    async def platesolve(self) -> m.PlateSolveResult:
        resp = await self._get("/prepared-image/solve")
        if isinstance(resp, dict) and not resp.get("_error"):
            return m.PlateSolveResult(
                ok=True, solved=bool(resp.get("Success", True)),
                ra_hours=float((resp.get("Coordinates") or {}).get("RA") or 0),
                dec_degrees=float((resp.get("Coordinates") or {}).get("Dec") or 0),
                rotation=float(resp.get("PositionAngle") or 0))
        return m.PlateSolveResult(ok=False, error="NINA 板解算失败或无可解图像")

    async def get_conditions(self) -> dict:
        from gateway.sim import astro
        sun = astro.sun_altitude(self.s.site_lat, self.s.site_lng)
        safety = await self._simple_info("safetymonitor")
        return {"ok": True, "sun_altitude": round(sun, 2),
                "is_safe": bool(safety.get("IsSafe", True)) if safety else None}

    # -- 序列 / 构图 ------------------------------------------------------ #
    async def get_sequence(self) -> m.SequenceState:
        info = await self._get("/sequence/state")
        seq = m.SequenceState()
        if isinstance(info, dict) and not info.get("_error"):
            seq.running = bool(info.get("IsRunning"))
            seq.status = "running" if seq.running else "idle"
        return seq

    async def sequence_action(self, action: str, p: dict) -> dict:
        mp = {"start": "/sequence/start", "stop": "/sequence/stop", "reset": "/sequence/reset"}
        if action in mp:
            return _ok(await self._get(mp[action]))
        return {"ok": False, "error": f"live 未映射序列动作 {action}"}

    async def get_framing(self) -> m.FramingState:
        return m.FramingState()

    async def framing_action(self, action: str, p: dict) -> dict:
        if action == "set_coordinates":
            return _ok(await self._get("/framing/set-coordinates",
                                       ra=p.get("ra"), dec=p.get("dec")))
        if action == "slew_center":
            return _ok(await self._get("/framing/slew", option="center"))
        return {"ok": False, "error": "live 未映射"}

    # -- 事件桥接:NINA /v2/socket -> 本地 bus ---------------------------- #
    async def _ws_bridge(self) -> None:
        url = (self.s.nina_base_url.replace("http", "ws").rstrip("/")
               + self.s.nina_socket_path)
        while not self._stop:
            try:
                import websockets
                async with websockets.connect(url) as ws:
                    self.bus.publish("NINA-SOCKET-CONNECTED")
                    async for raw in ws:
                        with contextlib.suppress(Exception):
                            data = json.loads(raw)
                            resp = data.get("Response", data)
                            evt = resp.get("Event") if isinstance(resp, dict) else None
                            if evt:
                                self.bus.publish(evt, data=resp)
            except Exception:
                await asyncio.sleep(5.0)


def _ok(resp: Any) -> dict:
    if isinstance(resp, dict) and resp.get("_error"):
        return {"ok": False, "error": resp["_error"]}
    return {"ok": True, "response": resp}
