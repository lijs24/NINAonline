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
import math
from typing import Any

import httpx

from config import Settings
from events import EventBus
from gateway import models as m
from gateway.base import NinaGateway
from gateway.sim import astro


def _f(v, default: float = 0.0) -> float:
    """安全解析浮点:NINA 对不可用值会返回字符串 "NaN" 或 None,
    直接 float() 会得到 nan/抛错并污染 JSON。一律折回 default。"""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    return default if (math.isnan(x) or math.isinf(x)) else x


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
        # 站点缓存:get_mount 读到 NINA 真实台址后更新,供 get_conditions 算日高度
        self._site = (settings.site_lat, settings.site_lng)
        self._ws_task: asyncio.Task | None = None
        self._stop = False
        self._image_seq = 0          # 每收到一次 IMAGE-PREPARED 自增 → 作为"新图就绪"的真实信号

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
            # DeviceId 与 list-devices 的 id 一致,供前端下拉精确选中已连接驱动
            driver_id = str(info.get("DeviceId", "")) if ok else ""
            return m.DeviceSummary(type=t, connected=conn, name=name, driver_id=driver_id,
                                   state="idle" if conn else "disconnected")
        return list(await asyncio.gather(*[one(t) for t in m.ALL_DEVICE_TYPES]))

    # -- 相机 ------------------------------------------------------------- #
    async def get_camera(self) -> m.CameraState:
        info = await self._get("/equipment/camera/info")
        c = m.CameraState()
        if isinstance(info, dict) and not info.get("_error"):
            c.connected = bool(info.get("Connected"))
            c.name = info.get("Name", "")
            c.temperature = _f(info.get("Temperature") or 0)
            # NINA 有 TargetTemp(制冷目标) 与 TemperatureSetPoint(驱动设定),优先前者
            c.target_temperature = _f(info.get("TargetTemp", info.get("TemperatureSetPoint")))
            c.cooler_on = bool(info.get("CoolerOn"))
            c.cooler_power = _f(info.get("CoolerPower") or 0)
            c.gain = int(info.get("Gain") or 0)
            c.offset = int(info.get("Offset") or 0)
            c.chip_width = int(info.get("XSize") or 0)
            c.chip_height = int(info.get("YSize") or 0)
            c.pixel_size_um = _f(info.get("PixelSize") or 0)
            c.has_cooler = bool(info.get("CanSetTemperature"))
            c.is_exposing = bool(info.get("IsExposing"))
            c.state = "exposing" if c.is_exposing else "idle"
        return c

    @staticmethod
    def _binstr(v) -> str:
        n = max(1, int(v))
        return f"{n}x{n}"

    async def camera_action(self, action: str, p: dict) -> dict:
        if action == "capture":
            # NINA 拍摄用当前 binning,故 bin 先经 set-binning 落地,再启动异步曝光;
            # gain 随拍摄参数下发(NINA 无独立 set-gain);getResult=false → 立即返回,
            # 完成由 WS 事件(API-CAPTURE-FINISHED / IMAGE-SAVE)通知,前端再拉预览。
            if p.get("bin"):
                await self._get("/equipment/camera/set-binning", binning=self._binstr(p["bin"]))
            params = {"duration": p.get("exposure"), "gain": p.get("gain"),
                      "imageType": p.get("image_type") or "SNAPSHOT",
                      "solve": "false", "save": str(bool(p.get("save", False))).lower(),
                      "getResult": "false",
                      # 不让 NINA 自动拉伸:出"线性"预备图,拉伸交给前端客户端(对标 asiair)。
                      # 否则 NINA 会归一化亮度,前端直方图无法随曝光真实偏移。
                      "skipAutoStretch": "true"}
            params = {k: v for k, v in params.items() if v is not None}
            return _ok(await self._get("/equipment/camera/capture", **params))
        if action == "abort":
            return _ok(await self._get("/equipment/camera/abort-exposure"))
        if action == "cool":
            return _ok(await self._get("/equipment/camera/cool",
                                       temperature=p.get("temperature"),
                                       minutes=p.get("minutes", -1), cancel="false"))
        if action == "warm":
            return _ok(await self._get("/equipment/camera/warm",
                                       minutes=p.get("minutes", -1), cancel="false"))
        if action == "set_control":
            name, val = p.get("name"), p.get("value")
            if name in ("bin", "Bin", "Binning"):
                return _ok(await self._get("/equipment/camera/set-binning", binning=self._binstr(val)))
            if name in ("Readout", "ReadoutMode"):
                return _ok(await self._get("/equipment/camera/set-readout", mode=int(val)))
            if name in ("USB", "USBLimit"):
                return _ok(await self._get("/equipment/camera/usb-limit", limit=int(val)))
            if name == "DewHeater":
                return _ok(await self._get("/equipment/camera/dew-heater",
                                           power=str(bool(int(val))).lower()))
            if name in ("TargetTemp", "Temperature"):
                return _ok(await self._get("/equipment/camera/cool",
                                           temperature=float(val), minutes=-1, cancel="false"))
            if name == "CoolerOn":
                if int(val):
                    info = await self._get("/equipment/camera/info")
                    tt = _f((info or {}).get("TargetTemp", (info or {}).get("TemperatureSetPoint")))
                    return _ok(await self._get("/equipment/camera/cool",
                                               temperature=tt, minutes=-1, cancel="false"))
                return _ok(await self._get("/equipment/camera/warm", minutes=-1, cancel="false"))
            if name in ("Gain", "Offset"):
                # NINA Advanced API 无独立 set-gain/offset:Gain 随拍摄参数下发;
                # Offset 由 NINA Profile 决定,接口不支持动态设置 —— 不报错,如实说明。
                return {"ok": True, "note": "增益随拍摄下发;偏置经 NINA Profile,接口不支持动态设置"}
            return {"ok": False, "error": f"live 未映射相机控制项 {name}"}
        return {"ok": False, "error": f"live 未映射相机动作 {action}"}

    async def get_image_meta(self, image_id=None) -> m.ImageMeta | None:
        # 优先用已保存历史的最后一张;无历史(快照未保存)则用曝光结束时间做"新图"标记 + 抓拍统计
        info = await self._get("/image-history", all="true")
        if isinstance(info, list) and info:
            last = info[-1]
            return m.ImageMeta(
                image_id=len(info) - 1, width=0, height=0,
                exposure_s=_f(last.get("ExposureTime") or 0),
                gain=int(last.get("Gain") or 0), offset=int(last.get("Offset") or 0),
                bin=1, filter=last.get("Filter", ""), hfr=_f(last.get("HFR") or 0),
                stars=int(last.get("Stars") or 0), captured_at=last.get("Date", ""),
                image_url="/api/camera/image")
        # 快照(未保存):只在真正收到 IMAGE-PREPARED(_image_seq 前进)后才报新图。
        # 切忌用 ExposureEndTime —— NINA 在曝光"开始"就把它设为预计结束时间,会早报一拍
        #(表现为"拍第二张时才刷出第一张")。
        if self._image_seq > 0:
            cam = await self._get("/equipment/camera/info")
            stats = await self._get("/equipment/camera/capture/statistics")
            c = cam if isinstance(cam, dict) and not cam.get("_error") else {}
            s = stats if isinstance(stats, dict) and not stats.get("_error") else {}
            return m.ImageMeta(
                image_id=self._image_seq,                 # 仅图像就绪时前进 → 前端据此刷新,不早报
                width=int(c.get("XSize") or 0), height=int(c.get("YSize") or 0),
                exposure_s=0.0, gain=int(c.get("Gain") or 0),
                offset=int(c.get("Offset") or 0), bin=int(c.get("BinX") or 1),
                filter="", hfr=_f(s.get("HFR") or 0),
                stars=int(s.get("Stars") or s.get("DetectedStars") or 0),
                captured_at=str(c.get("ExposureEndTime") or ""), image_url="/api/camera/image")
        return None

    async def get_image_png(self, image_id=None, stretch=True) -> bytes | None:
        # 快照走 /prepared-image(最近一张已处理图,直接回原始字节);已保存图走 /image/{idx}
        # (stream 省略时返回 base64 信封)。两端点返回形态不同,故按 content-type 分别处理。
        import base64
        # autoPrepare=false → 取"线性"图(不在服务端再拉伸),前端据此算直方图(随曝光真实偏移)+ 自行拉伸显示。
        # 回退到 autoPrepare=true(已拉伸)以防线性取不到。
        attempts = [("/prepared-image", {"resize": "true", "autoPrepare": "false"}),
                    ("/prepared-image", {"resize": "true", "autoPrepare": "true"})]
        if image_id is not None:
            attempts.append((f"/image/{image_id}", {"resize": "true", "autoPrepare": "false"}))
        attempts.append(("/image/0", {"resize": "true", "autoPrepare": "true"}))
        for path, params in attempts:
            try:
                r = await self._client.get(self._api + path, params=params)
                if r.status_code != 200:
                    continue
                if r.headers.get("content-type", "").startswith("image/"):
                    return r.content                     # 原始 PNG/JPEG 字节
                data = r.json()                          # base64 信封 {Response: "..."}
                b64 = data.get("Response") if isinstance(data, dict) else None
                if isinstance(b64, str) and b64:
                    return base64.b64decode(b64)
            except Exception:
                continue
        return None

    # -- 赤道仪 ----------------------------------------------------------- #
    async def get_mount(self) -> m.MountState:
        info = await self._get("/equipment/mount/info")
        mo = m.MountState(site_lat=self.s.site_lat, site_lng=self.s.site_lng)
        if isinstance(info, dict) and not info.get("_error"):
            mo.connected = bool(info.get("Connected"))
            mo.name = info.get("Name", "")
            mo.ra_hours = _f(info.get("RightAscension"))
            mo.dec_degrees = _f(info.get("Declination"))
            mo.tracking = bool(info.get("TrackingEnabled"))
            mo.slewing = bool(info.get("Slewing"))
            mo.at_park = bool(info.get("AtPark"))
            mo.at_home = bool(info.get("AtHome"))
            mo.side_of_pier = str(info.get("SideOfPier", "unknown")).lower().replace("pier", "")
            mo.altitude = _f(info.get("Altitude"))
            mo.azimuth = _f(info.get("Azimuth"))
            mo.lst_hours = _f(info.get("SiderealTime"))
            mo.time_to_meridian_h = _f(info.get("TimeToMeridianFlip"))
            # 真实台址(NINA 报告)覆盖配置默认,并缓存供 get_conditions
            lat, lng = _f(info.get("SiteLatitude")), _f(info.get("SiteLongitude"))
            if lat or lng:
                mo.site_lat, mo.site_lng = lat, lng
                mo.site_elev = _f(info.get("SiteElevation"))
                self._site = (lat, lng)
            # NINA 未连接时不返回 RA/Dec 字符串,用数值兜底成雕版样式
            mo.ra_text = info.get("RightAscensionString") or astro.hours_to_hms(mo.ra_hours)
            mo.dec_text = info.get("DeclinationString") or astro.deg_to_dms(mo.dec_degrees)
            mo.lst_text = info.get("SiderealTimeString") or astro.hours_to_hms(mo.lst_hours)
        return mo

    async def _slew_safety(self, ra, dec) -> dict | None:
        """转向护栏(防打腿):slew/goto 前校验目标安全。返回 None=放行,否则返回拒绝响应。
        规则(均 env 可调,见 config):
          1) 目标地平高度 < mount_min_alt_deg → 拒绝(防低空撞腿/朝地)。
          2) mount_meridian_limit_deg>0 时,目标在当前墩侧越过中天进入配重上扬区超过该角度 → 拒绝。
        读不到赤道仪状态一律拒绝(fail-safe)。park/home/stop/tracking/sync/flip 不经此校验。"""
        def _num(v):
            try:
                f = float(v)
            except (TypeError, ValueError):
                return None
            return f if math.isfinite(f) else None
        # 1) 目标坐标必须合法:RA∈[0,24) Dec∈[-90,90];缺失/越界/非数 → fail-safe 拒绝
        ra, dec = _num(ra), _num(dec)
        if ra is None or dec is None or not (0 <= ra < 24) or not (-90 <= dec <= 90):
            return {"ok": False, "blocked": True, "error": "护栏:slew 目标坐标缺失/越界,已拒绝"}
        info = await self._get("/equipment/mount/info")
        if not isinstance(info, dict) or info.get("_error") or not info.get("Connected"):
            return {"ok": False, "blocked": True, "error": "护栏:读不到赤道仪状态,安全起见拒绝转向"}
        # 2) 站纬/恒星时必须有效(不再用 _f 折成 0 蒙混);无效 → 无法核算高度 → 拒绝
        lat, lst = _num(info.get("SiteLatitude")), _num(info.get("SiderealTime"))
        if lat is None or not (-90 <= lat <= 90) or lst is None or not (0 <= lst < 24):
            return {"ok": False, "blocked": True,
                    "error": "护栏:赤道仪站纬/恒星时无效,无法核算安全高度 —— 拒绝转向"}
        ha_deg = (((lst - ra + 12) % 24) - 12) * 15.0      # >0=已过中天(西), <0=未过(东)
        ha, latr, decr = math.radians(ha_deg), math.radians(lat), math.radians(dec)
        sin_alt = math.sin(decr) * math.sin(latr) + math.cos(decr) * math.cos(latr) * math.cos(ha)
        alt = math.degrees(math.asin(max(-1.0, min(1.0, sin_alt))))
        if alt < self.s.mount_min_alt_deg:
            return {"ok": False, "blocked": True,
                    "error": f"护栏拦截:目标高度 {alt:.1f}° 低于安全下限 {self.s.mount_min_alt_deg:.0f}°,拒绝转向(防打腿)"}
        lim = self.s.mount_meridian_limit_deg
        if lim and lim > 0:
            side = str(info.get("SideOfPier", "")).lower().replace("pier", "")
            # 3) 中天限位启用但墩侧未知 → fail-closed(否则方向判断不可靠会放过危险侧)
            if side not in ("east", "west"):
                return {"ok": False, "blocked": True,
                        "error": "护栏:中天限位已启用但赤道仪墩侧(SideOfPier)未知 —— fail-safe 拒绝转向"}
            if (side == "west" and ha_deg > lim) or (side == "east" and ha_deg < -lim):
                return {"ok": False, "blocked": True,
                        "error": f"护栏拦截:目标过中天 {ha_deg:+.1f}°(墩侧={side},限位 {lim:.0f}°),"
                                 f"配重上扬/打腿风险 —— 拒绝转向,请先在 NINA 翻转或确认安全"}
        return None

    async def mount_action(self, action: str, p: dict) -> dict:
        if action == "slew":
            if (err := await self._slew_safety(p.get("ra"), p.get("dec"))) is not None:
                return err
            return _ok(await self._get("/equipment/mount/slew",
                                       ra=_f(p.get("ra")) * 15.0, dec=p.get("dec")))   # ninaAPI 写接口 RA 用「度」;本站存小时 → ×15
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
            return _ok(await self._get("/equipment/mount/sync", ra=_f(p.get("ra")) * 15.0, dec=p.get("dec")))   # RA 小时→度
        return {"ok": False, "error": f"live 未映射赤道仪动作 {action}"}

    # -- 调焦 ------------------------------------------------------------- #
    async def get_focuser(self) -> m.FocuserState:
        info = await self._get("/equipment/focuser/info")
        f = m.FocuserState()
        if isinstance(info, dict) and not info.get("_error"):
            f.connected = bool(info.get("Connected"))
            f.name = info.get("Name", "")
            f.position = int(info.get("Position") or 0)
            f.temperature = _f(info.get("Temperature") or 0)
            f.is_moving = bool(info.get("IsMoving"))
        return f

    async def get_autofocus(self) -> m.AutoFocusResult:
        info = await self._get("/equipment/focuser/last-af")
        af = m.AutoFocusResult()
        if isinstance(info, dict) and not info.get("_error"):
            for pt in info.get("MeasurePoints", []) or []:
                af.points.append(m.AutoFocusPoint(
                    position=int(pt.get("Position", 0)), hfr=_f(pt.get("Value", 0))))
            cm = info.get("CalculatedFocusPoint") or {}
            af.best_position = int(cm.get("Position", 0)) if cm else None
        return af

    async def focuser_action(self, action: str, p: dict) -> dict:
        if action == "move_abs":
            return _ok(await self._get("/equipment/focuser/move", position=int(p.get("position", 0))))
        if action == "move_rel":
            # NINA 只接受绝对位:取当前位 + 相对步进
            info = await self._get("/equipment/focuser/info")
            cur = int((info or {}).get("Position") or 0) if isinstance(info, dict) else 0
            return _ok(await self._get("/equipment/focuser/move", position=cur + int(p.get("steps", 0))))
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
            fw.is_moving = bool(info.get("IsMoving"))
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
            g.pixel_scale = _f(info.get("PixelScale") or 1)
            rms = info.get("RMSError") or {}
            if rms:
                g.rms_ra = _f((rms.get("RA") or {}).get("Arcseconds") or 0)
                g.rms_dec = _f((rms.get("Dec") or {}).get("Arcseconds") or 0)
                g.rms_total = _f((rms.get("Total") or {}).get("Arcseconds") or 0)
            # PHD2 逐帧指标:NINA 把它聚合进 LastGuideStep
            ls = info.get("LastGuideStep") or {}
            if ls:
                g.snr = _f(ls.get("SNR"))
                g.star_mass = _f(ls.get("StarMass"))
                g.hfd = _f(ls.get("HFD"))
                g.avg_dist = _f(ls.get("AvgDist"))
        if g.connected:                       # 导星曝光由 PHD2 直读(NINA 不暴露)
            ex = await self._phd2_rpc("get_exposure")
            if isinstance(ex, dict) and isinstance(ex.get("result"), (int, float)):
                g.exposure = int(ex["result"])
        return g

    async def get_guider_graph(self) -> list[m.GuideStep]:
        info = await self._get("/equipment/guider/graph")
        steps = []
        if isinstance(info, dict):
            for i, pt in enumerate(info.get("GuideSteps", []) or []):
                steps.append(m.GuideStep(
                    t=_f(i), ra_raw=_f(pt.get("RADistanceRaw") or 0),
                    dec_raw=_f(pt.get("DECDistanceRaw") or 0),
                    ra_dist=_f(pt.get("RADistanceRaw") or 0),
                    dec_dist=_f(pt.get("DECDistanceRaw") or 0)))
        return steps

    async def _phd2_cmd(self, method: str, params=None) -> dict:
        """跑一条 PHD2 命令并归一成 {ok, error, result}。"""
        r = await self._phd2_rpc(method, params)
        if r is None:
            return {"ok": False, "error": "PHD2 不可达(检查 PHD2 服务器是否开启/已连接)"}
        if isinstance(r, dict) and r.get("error"):
            e = r["error"]; msg = (e.get("message") if isinstance(e, dict) else str(e)) or "失败"
            return {"ok": False, "error": f"PHD2:{msg}"}
        return {"ok": True, "result": (r or {}).get("result")}

    async def guider_action(self, action: str, p: dict) -> dict:
        # PHD2 直控,对应 PHD2 面板按钮;clear_calibration 仍走 NINA。
        SETTLE = {"pixels": 1.5, "time": 8, "timeout": 40}
        if action == "loop":                        # 开始连续曝光
            return await self._phd2_cmd("loop")
        if action in ("auto_select", "find_star"):  # 自动选星(需正在 Looping)
            r = await self._phd2_cmd("find_star")
            if not r["ok"] and "find star" in (r.get("error") or "").lower():
                r["error"] = "PHD2 找不到星(确认正在连续曝光、画面有可选星)"
            elif r["ok"]:
                r["star_pos"] = r.get("result")
            return r
        if action in ("start", "guide"):            # 开始导星(自动选星+校准+导星+稳定)
            return await self._phd2_cmd("guide", {"settle": SETTLE, "recalibrate": bool(p.get("recalibrate", False))})
        if action in ("stop", "stop_capture"):      # 停止拍照/导星(停 looping + guiding)
            return await self._phd2_cmd("stop_capture")
        if action == "dither":                       # 抖动
            return await self._phd2_cmd("dither", {"amount": _f(p.get("amount", 3)) or 3.0,
                                                   "raOnly": bool(p.get("ra_only", False)), "settle": SETTLE})
        if action == "set_exposure":                 # 设置导星曝光(毫秒)
            ms = int(_f(p.get("ms") if p.get("ms") is not None else p.get("exposure")))
            if ms <= 0:
                return {"ok": False, "error": "曝光时间(ms)无效"}
            return await self._phd2_cmd("set_exposure", [ms])
        if action == "clear_calibration":
            return _ok(await self._get("/equipment/guider/clear-calibration"))
        return {"ok": False, "error": f"live 未映射导星动作 {action}"}

    # -- 导星画面:直连 PHD2 事件服务器(TCP),行分隔 JSON-RPC ------------- #
    async def _phd2_rpc(self, method: str, params=None, timeout: float = 3.0) -> dict | None:
        """与 PHD2 跑一次请求/响应。连接后 PHD2 会先推一串事件行(Version/AppState…),
        我们发出请求后逐行读、跳过事件行,直到读到 id 匹配的响应。短连接、即用即关:
        PHD2 支持多客户端并存(NINA 也连着),不影响导星。"""
        import uuid
        host, port = self.s.phd2_host, self.s.phd2_port
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=timeout)
        except (OSError, asyncio.TimeoutError):
            return None        # PHD2 不可达 / 服务器未开
        rid = uuid.uuid4().hex
        req = {"method": method, "id": rid}
        if params is not None:
            req["params"] = params
        try:
            writer.write((json.dumps(req) + "\r\n").encode("utf-8"))
            await writer.drain()

            async def _read_until():
                while True:
                    line = await reader.readline()
                    if not line:
                        return None        # 对端关闭
                    line = line.strip()
                    if not line.startswith(b"{"):
                        continue
                    try:
                        o = json.loads(line)
                    except ValueError:
                        continue
                    if o.get("Event") is not None:
                        continue           # 事件行,跳过
                    if str(o.get("id")) == rid:
                        return o

            return await asyncio.wait_for(_read_until(), timeout=timeout)
        except (OSError, asyncio.TimeoutError):
            return None
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def get_guider_star_image(self) -> dict:
        import base64
        import numpy as np
        from gateway.sim import imaging

        resp = await self._phd2_rpc("get_star_image", params=[self.s.phd2_star_size])
        if resp is None:
            return {"available": False, "reason": "PHD2 不可达(服务器未开或未连接)"}
        if "result" not in resp:
            # 多半是"未选星":PHD2 在未导星/未锁定星点时对 get_star_image 返回 error
            err = (resp.get("error") or {}).get("message") if isinstance(resp.get("error"), dict) else None
            return {"available": False, "reason": err or "PHD2 当前未跟踪星点"}
        r = resp["result"]
        try:
            w, h = int(r["width"]), int(r["height"])
            raw = base64.b64decode(r["pixels"])
            arr = np.frombuffer(raw, dtype="<u2")[:w * h].reshape(h, w)
            png = imaging.stretch_guide_png(arr)
            sp = r.get("star_pos")
            if not (isinstance(sp, (list, tuple)) and len(sp) >= 2):
                sp = [w / 2.0, h / 2.0]      # 畸形/缺失 → 居中
            star = [float(sp[0]), float(sp[1])]
        except (KeyError, ValueError, TypeError, IndexError):
            return {"available": False, "reason": "PHD2 画面数据无法解析"}
        return {
            "available": True,
            "frame": r.get("frame"),
            "width": w, "height": h,
            "star": star,
            "image": "data:image/png;base64," + base64.b64encode(png).decode("ascii"),
        }

    # -- 其余设备:尽力读 info,动作多数未在 NINA API 暴露 ----------------- #
    async def _simple_info(self, dev: str) -> dict:
        info = await self._get(f"/equipment/{dev}/info")
        return info if isinstance(info, dict) and not info.get("_error") else {}

    async def get_rotator(self) -> m.RotatorState:
        i = await self._simple_info("rotator")
        return m.RotatorState(connected=bool(i.get("Connected")), name=i.get("Name", ""),
                              position=_f(i.get("Position") or 0))

    async def get_dome(self) -> m.DomeState:
        i = await self._simple_info("dome")
        return m.DomeState(connected=bool(i.get("Connected")), name=i.get("Name", ""),
                           azimuth=_f(i.get("Azimuth") or 0))

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
                              temperature=_f(i.get("Temperature") or 0),
                              humidity=_f(i.get("Humidity") or 0),
                              cloud_cover=_f(i.get("CloudCover") or 0))

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
        # ninaAPI 2.2.11.x 没有 /prepared-image/solve(2.2.15 才加),用旧版也支持的
        # capture+solve+omitImage:拍摄即解算、只回解算结果不含图,新旧版本都兼容。
        # 拍摄+解算耗时长(~15-20s),单独用长超时,不走默认 8s 的 _client 超时。
        try:
            r = await self._client.get(
                self._api + "/equipment/camera/capture",
                params={"solve": "true", "omitImage": "true", "getResult": "true"},
                timeout=httpx.Timeout(90.0, connect=5.0))
            data = r.json()
        except Exception as e:
            return m.PlateSolveResult(ok=False, solved=False, error=f"板解算调用失败:{e}")
        if not (isinstance(data, dict) and data.get("Success")):
            err = data.get("Error") if isinstance(data, dict) else None
            return m.PlateSolveResult(ok=False, solved=False, error=err or "NINA 板解算调用失败")
        psr = ((data.get("Response") or {}).get("PlateSolveResult")) or {}
        if psr.get("Success"):
            c = psr.get("Coordinates") or {}
            return m.PlateSolveResult(
                ok=True, solved=True,
                ra_hours=_f(c.get("RA") or 0),
                dec_degrees=_f(c.get("Dec") or 0),
                rotation=_f(psr.get("PositionAngle") or 0))
        return m.PlateSolveResult(ok=False, solved=False, error="解算未成功(星点不足/无解)")

    async def get_site(self) -> m.Site:
        """从 NINA GET /profile/show 读 AstrometrySettings 的台址。
        读不到时回落配置默认(同 get_mount 缓存的 _site)。"""
        prof = await self._get("/profile/show")
        if isinstance(prof, dict) and not prof.get("_error"):
            astr = prof.get("AstrometrySettings") or {}
            lat = _f(astr.get("Latitude"), self.s.site_lat)
            lon = _f(astr.get("Longitude"), self.s.site_lng)
            elev = _f(astr.get("Elevation"), self.s.site_elev)
            if lat or lon:
                self._site = (lat, lon)
            return m.Site(lat=lat, lon=lon, elev=elev)
        return m.Site(lat=self.s.site_lat, lon=self.s.site_lng, elev=self.s.site_elev)

    async def get_conditions(self) -> dict:
        sun = astro.sun_altitude(self._site[0], self._site[1])
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
        # 远程启动序列默认禁用:序列内 GOTO 在 NINA 内部执行,不经本站 _slew_safety 护栏
        if action == "start" and not self.s.allow_sequence_start:
            return {"ok": False, "blocked": True,
                    "error": "护栏:远程启动序列默认禁用 —— 序列内 GOTO 不经本站赤道仪护栏,"
                             "只能靠 NINA 自身中天翻转/限位。确需远程启动请设 NINAWEB_ALLOW_SEQUENCE_START=1 "
                             "并确认 NINA 已配好安全机制"}
        if action in mp:
            return _ok(await self._get(mp[action]))
        if action == "set_plan":
            return {"ok": False, "unsupported": True,
                    "error": "live 暂不支持从本站保存序列计划 —— 请在 NINA 端编排/加载序列(本站仅 start/stop/reset)"}
        return {"ok": False, "error": f"live 未映射序列动作 {action}"}

    async def get_framing(self) -> m.FramingState:
        return m.FramingState()

    async def framing_action(self, action: str, p: dict) -> dict:
        if action == "set_coordinates":
            self._framing_target = (p.get("ra"), p.get("dec"))   # 记住构图目标,供 slew_center 护栏核算
            return _ok(await self._get("/framing/set-coordinates",
                                       RAangle=_f(p.get("ra")) * 15.0, DecAngle=p.get("dec")))   # 参数名 RAangle/DecAngle;RA 度
        if action == "slew_center":
            # 构图页"转向并居中"会移动赤道仪 → 必须经 _slew_safety(防绕过护栏打腿)
            tgt = getattr(self, "_framing_target", None)
            if not tgt or tgt[0] is None or tgt[1] is None:
                return {"ok": False, "blocked": True,
                        "error": "护栏:未知构图目标坐标,请先「设置构图坐标」再转向(防打腿)"}
            if (err := await self._slew_safety(tgt[0], tgt[1])) is not None:
                return err
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
                                if evt in ("IMAGE-PREPARED", "IMAGE-SAVE"):
                                    self._image_seq += 1   # 图像真正就绪
                                self.bus.publish(evt, domain=_evt_domain(evt), data=resp)
            except Exception:
                await asyncio.sleep(5.0)


def _ok(resp: Any) -> dict:
    if isinstance(resp, dict) and resp.get("_error"):
        return {"ok": False, "error": resp["_error"]}
    return {"ok": True, "response": resp}


# NINA 事件名 → 设备域(前端按 e.domain 决定刷新哪页;NINA 事件不带域,据名前缀推)
_EVT_DOMAIN = (
    ("CAMERA", "camera"), ("IMAGE", "camera"), ("API-CAPTURE", "camera"),
    ("AUTOFOCUS", "focuser"), ("FOCUSER", "focuser"),
    ("MOUNT", "mount"), ("FILTERWHEEL", "filterwheel"), ("GUIDER", "guider"),
    ("ROTATOR", "rotator"), ("DOME", "dome"), ("FLAT", "flatdevice"),
    ("SWITCH", "switch"), ("WEATHER", "weather"), ("SAFETY", "safetymonitor"),
    ("SEQUENCE", "sequence"), ("PLATESOLVE", "camera"),
)


def _evt_domain(evt: str) -> str:
    e = (evt or "").upper()
    for pre, dom in _EVT_DOMAIN:
        if e.startswith(pre):
            return dom
    return ""
