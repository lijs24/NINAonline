"""SimGateway —— 完整的模拟设备世界。

实现 NinaGateway:所有设备都是状态机,一个 ~10Hz 的 tick 循环推进"物理"
(赤道仪 slew/跟踪、调焦移动、制冷趋近、导星误差、天气抖动),长流程
(自动对焦 V 曲线、序列执行)作为独立 asyncio 任务驱动并发出 NINA 式事件。
"""
from __future__ import annotations

import asyncio
import math
import random
from datetime import datetime, timezone

from config import Settings
from events import EventBus
from gateway import models as m
from gateway.base import NinaGateway
from gateway.sim import astro, imaging


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# 简易深空目标表(构图检索 + GOTO 用)
CATALOG: list[dict] = [
    {"name": "M8 礁湖星云", "type": "弥漫星云", "ra": 18.06, "dec": -24.38, "mag": 6.0, "size": 90, "cat": "Messier"},
    {"name": "M42 猎户星云", "type": "弥漫星云", "ra": 5.59, "dec": -5.39, "mag": 4.0, "size": 85, "cat": "Messier"},
    {"name": "M31 仙女星系", "type": "旋涡星系", "ra": 0.71, "dec": 41.27, "mag": 3.4, "size": 178, "cat": "Messier"},
    {"name": "M45 昴星团", "type": "疏散星团", "ra": 3.79, "dec": 24.10, "mag": 1.6, "size": 110, "cat": "Messier"},
    {"name": "M51 涡状星系", "type": "旋涡星系", "ra": 13.50, "dec": 47.20, "mag": 8.4, "size": 11, "cat": "Messier"},
    {"name": "M16 鹰状星云", "type": "发射星云", "ra": 18.31, "dec": -13.79, "mag": 6.0, "size": 35, "cat": "Messier"},
    {"name": "M17 欧米伽星云", "type": "发射星云", "ra": 18.34, "dec": -16.18, "mag": 6.0, "size": 11, "cat": "Messier"},
    {"name": "M27 哑铃星云", "type": "行星状星云", "ra": 19.99, "dec": 22.72, "mag": 7.5, "size": 8, "cat": "Messier"},
    {"name": "M81 波德星系", "type": "旋涡星系", "ra": 9.93, "dec": 69.07, "mag": 6.9, "size": 27, "cat": "Messier"},
    {"name": "M101 风车星系", "type": "旋涡星系", "ra": 14.05, "dec": 54.35, "mag": 7.9, "size": 29, "cat": "Messier"},
    {"name": "M13 武仙座球状星团", "type": "球状星团", "ra": 16.69, "dec": 36.46, "mag": 5.8, "size": 20, "cat": "Messier"},
    {"name": "NGC7000 北美洲星云", "type": "发射星云", "ra": 20.98, "dec": 44.53, "mag": 4.0, "size": 120, "cat": "NGC"},
    {"name": "NGC6960 女巫扫帚", "type": "超新星遗迹", "ra": 20.76, "dec": 30.71, "mag": 7.0, "size": 70, "cat": "NGC"},
    {"name": "IC1805 心脏星云", "type": "发射星云", "ra": 2.55, "dec": 61.45, "mag": 6.5, "size": 150, "cat": "IC"},
    {"name": "IC434 马头星云", "type": "暗星云", "ra": 5.68, "dec": -2.46, "mag": 6.8, "size": 30, "cat": "IC"},
]

DEFAULT_FILTERS = [
    ("L", 0), ("R", -120), ("G", -110), ("B", -90),
    ("Ha", 350), ("OIII", 340), ("SII", 345),
]


class SimGateway(NinaGateway):
    mode = "sim"

    def __init__(self, settings: Settings, bus: EventBus) -> None:
        self.s = settings
        self.bus = bus
        self._tick_task: asyncio.Task | None = None
        self._bg: set[asyncio.Task] = set()
        self._stop = False

        # 设备模型
        self.camera = m.CameraState()
        self.mount = m.MountState(site_lat=settings.site_lat, site_lng=settings.site_lng,
                                  site_elev=settings.site_elev)
        self.focuser = m.FocuserState()
        self.autofocus = m.AutoFocusResult()
        self.filterwheel = m.FilterWheelState()
        self.guider = m.GuiderState()
        self.rotator = m.RotatorState()
        self.dome = m.DomeState()
        self.flat = m.FlatDeviceState()
        self.switch = m.SwitchState()
        self.weather = m.WeatherState()
        self.safety = m.SafetyState()
        self.sequence = m.SequenceState()
        self.framing = m.FramingState()

        # 模拟内部量
        self._guide_steps: list[m.GuideStep] = []
        self._guide_t0 = 0.0
        self._images: dict[int, dict] = {}       # id -> 生成参数(按需重渲染,省内存)
        self._metas: list[m.ImageMeta] = []
        self._image_counter = 0
        self._best_focus = 14300                 # 最佳焦点位置(随温度漂移)
        self._best_focus_ref_temp = 20.0
        self._focus_target = 14300
        self._fw_target = 0
        self._slew_target: tuple[float, float] | None = None
        self._mount_altaz_hold: tuple[float, float] | None = None
        self._abort_capture = False
        self._capture_task: asyncio.Task | None = None
        self._af_task: asyncio.Task | None = None
        self._seq_task: asyncio.Task | None = None
        self._seq_stop = False

    # ====================================================================== #
    # 生命周期
    # ====================================================================== #
    async def start(self) -> None:
        self._tick_task = asyncio.create_task(self._tick_loop())

    async def stop(self) -> None:
        self._stop = True
        for t in [self._tick_task, *self._bg]:
            if t:
                t.cancel()

    def _spawn(self, coro) -> asyncio.Task:
        t = asyncio.create_task(coro)
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)
        return t

    # ====================================================================== #
    # 设备生命周期
    # ====================================================================== #
    async def list_drivers(self, device_type: str) -> list[m.DriverDescriptor]:
        sim = m.DriverDescriptor(id=f"sim.{device_type}", name=f"模拟{device_type}",
                                 category="Simulator",
                                 description="内置模拟驱动,无需硬件")
        ascom = m.DriverDescriptor(id=f"ascom.{device_type}", name=f"ASCOM {device_type} (未接入)",
                                   category="ASCOM", description="占位:provider=live 时由 NINA 提供")
        return [sim, ascom]

    async def connect(self, device_type: str, driver_id: str | None) -> dict:
        st = self._state(device_type)
        if st is None:
            return {"ok": False, "error": f"未知设备类型 {device_type}"}
        st.connected = True
        self._setup_device(device_type)
        self.bus.publish(f"{self._evt(device_type)}-CONNECTED", domain=device_type)
        self.bus.state_changed(device_type)
        return {"ok": True, "type": device_type, "name": st.name}

    async def disconnect(self, device_type: str) -> dict:
        st = self._state(device_type)
        if st is None:
            return {"ok": False, "error": f"未知设备类型 {device_type}"}
        st.connected = False
        self.bus.publish(f"{self._evt(device_type)}-DISCONNECTED", domain=device_type)
        self.bus.state_changed(device_type)
        return {"ok": True}

    def _state(self, t: str):
        return {
            "camera": self.camera, "mount": self.mount, "focuser": self.focuser,
            "filterwheel": self.filterwheel, "guider": self.guider, "rotator": self.rotator,
            "dome": self.dome, "flatdevice": self.flat, "switch": self.switch,
            "weather": self.weather, "safetymonitor": self.safety,
        }.get(t)

    @staticmethod
    def _evt(t: str) -> str:
        return {"camera": "CAMERA", "mount": "MOUNT", "focuser": "FOCUSER",
                "filterwheel": "FILTERWHEEL", "guider": "GUIDER", "rotator": "ROTATOR",
                "dome": "DOME", "flatdevice": "FLAT", "switch": "SWITCH",
                "weather": "WEATHER", "safetymonitor": "SAFETY"}.get(t, t.upper())

    def _setup_device(self, t: str) -> None:
        if t == "camera":
            c = self.camera
            c.name = "ZWO ASI6200MM Pro (模拟)"
            c.sensor_name = "IMX455"
            c.chip_width, c.chip_height = 9576, 6388
            c.pixel_size_um = 3.76
            c.bit_depth = 16
            c.has_cooler = True
            c.is_color = False
            c.temperature = 18.0
            c.target_temperature = -10.0
            c.gain, c.offset = 100, 50
            c.exposure_s = 3.0
            c.subframe = {"x": 0, "y": 0, "width": c.chip_width, "height": c.chip_height}
            c.readout_modes = ["Normal", "High Speed"]
            self._refresh_camera_controls()
        elif t == "mount":
            mo = self.mount
            mo.name = "Mount Simulator (赤道仪)"
            mo.capabilities = ["position", "goto", "sync", "park", "home", "track",
                               "track_mode", "move", "pulse", "flip"]
            mo.at_park = True
            mo.ra_hours = astro.lst_hours(mo.site_lng)        # 停靠在天顶/极轴附近
            mo.dec_degrees = 89.9
            self._recompute_mount()
        elif t == "focuser":
            f = self.focuser
            f.name = "Focuser Simulator (EAF)"
            f.max_step = 30000
            f.position = 14300
            f.temperature = 12.0
            self._focus_target = f.position
        elif t == "filterwheel":
            fw = self.filterwheel
            fw.name = "EFW 7×36mm (模拟)"
            fw.filters = [m.FilterSlot(position=i, name=n, focus_offset=o)
                          for i, (n, o) in enumerate(DEFAULT_FILTERS)]
            fw.position = 0
            self._fw_target = 0
        elif t == "guider":
            g = self.guider
            g.name = "PHD2 Simulator (导星)"
            g.pixel_scale = 1.25
        elif t == "rotator":
            self.rotator.name = "Rotator Simulator"
        elif t == "dome":
            self.dome.name = "Dome Simulator"
        elif t == "flatdevice":
            self.flat.name = "Flat Panel Simulator"
            self.flat.max_brightness = 100
        elif t == "switch":
            self.switch.name = "Switch Hub Simulator"
            self.switch.switches = [
                m.SwitchItem(id=0, name="主镜加热带", value=0, writable=True),
                m.SwitchItem(id=1, name="相机电源", value=1, writable=True),
                m.SwitchItem(id=2, name="赤道仪电源", value=1, writable=True),
                m.SwitchItem(id=3, name="露点", value=4.2, min=-20, max=40, writable=False),
            ]
        elif t == "weather":
            w = self.weather
            w.name = "Weather Simulator"
            w.temperature, w.humidity, w.pressure = 6.0, 55.0, 845.0
            w.wind_speed, w.cloud_cover, w.dew_point, w.sky_quality = 2.4, 8.0, -2.0, 21.3
        elif t == "safetymonitor":
            self.safety.name = "Safety Monitor Simulator"
            self.safety.is_safe = True

    async def device_summaries(self) -> list[m.DeviceSummary]:
        out = []
        for t in m.ALL_DEVICE_TYPES:
            st = self._state(t)
            out.append(m.DeviceSummary(
                type=t, connected=st.connected, name=st.name,
                driver_id=(f"sim.{t}" if st.connected else ""),
                state=self._device_state_text(t), detail=self._device_detail(t)))
        return out

    def _device_state_text(self, t: str) -> str:
        st = self._state(t)
        if not st.connected:
            return "disconnected"
        return {
            "camera": self.camera.state,
            "mount": "slewing" if self.mount.slewing else ("tracking" if self.mount.tracking else "idle"),
            "focuser": "moving" if self.focuser.is_moving else "idle",
            "filterwheel": "moving" if self.filterwheel.is_moving else "idle",
            "guider": self.guider.state,
        }.get(t, "idle")

    def _device_detail(self, t: str) -> str:
        if not self._state(t).connected:
            return ""
        if t == "camera":
            return f"{self.camera.temperature:.1f}°C · Gain {self.camera.gain}"
        if t == "mount":
            return f"{self.mount.ra_text} {self.mount.dec_text}"
        if t == "focuser":
            return f"位置 {self.focuser.position} · {self.focuser.temperature:.1f}°C"
        if t == "filterwheel" and self.filterwheel.filters:
            return f"当前 {self.filterwheel.filters[self.filterwheel.position].name}"
        if t == "guider" and self.guider.state == "guiding":
            return f"RMS {self.guider.rms_total:.2f}\""
        return ""

    # ====================================================================== #
    # tick:推进物理
    # ====================================================================== #
    async def _tick_loop(self) -> None:
        dt = 1.0 / self.s.sim_tick_hz
        while not self._stop:
            try:
                self._tick(dt)
            except Exception as e:  # 不让单次异常杀掉循环
                self.bus.publish("ERROR-SIM", domain="", detail=str(e))
            await asyncio.sleep(dt)

    def _tick(self, dt: float) -> None:
        self._tick_mount(dt)
        self._tick_focuser(dt)
        self._tick_filterwheel(dt)
        self._tick_cooler(dt)
        self._tick_guider(dt)
        self._tick_weather(dt)
        self._tick_dome(dt)

    def _tick_mount(self, dt: float) -> None:
        mo = self.mount
        if not mo.connected:
            return
        if mo.slewing and self._slew_target is not None:
            tra, tdec = self._slew_target
            rate = 12.0 * dt          # 度/帧 ≈ 12°/s(演示用,够快又有过程感)
            d_ra = (tra - mo.ra_hours) * 15.0
            # RA 取最短路径
            d_ra = (d_ra + 180) % 360 - 180
            d_dec = tdec - mo.dec_degrees
            dist = math.hypot(d_ra, d_dec)
            if dist <= rate:
                mo.ra_hours, mo.dec_degrees = tra, tdec
                mo.slewing = False
                self._slew_target = None
                self._mount_altaz_hold = None
                self.bus.publish("MOUNT-SLEWED", domain="mount",
                                 ra=mo.ra_hours, dec=mo.dec_degrees, target=mo.target_name)
            else:
                step = rate / dist
                mo.ra_hours += (d_ra * step) / 15.0
                mo.dec_degrees += d_dec * step
        elif not mo.tracking and not mo.at_park:
            # 未跟踪:锁地平坐标,RA/Dec 随天球漂移(物理正确,刻意保留)
            if self._mount_altaz_hold is None:
                self._mount_altaz_hold = astro.radec_to_altaz(
                    mo.ra_hours, mo.dec_degrees, mo.site_lat, mo.site_lng)
        mo.is_moving = mo.slewing
        self._recompute_mount()

    def _recompute_mount(self) -> None:
        mo = self.mount
        mo.lst_hours = astro.lst_hours(mo.site_lng)
        mo.lst_text = astro.hours_to_hms(mo.lst_hours)
        mo.ra_hours %= 24.0
        mo.dec_degrees = max(-90.0, min(90.0, mo.dec_degrees))
        mo.ra_text = astro.hours_to_hms(mo.ra_hours)
        mo.dec_text = astro.deg_to_dms(mo.dec_degrees)
        alt, az = astro.radec_to_altaz(mo.ra_hours, mo.dec_degrees, mo.site_lat, mo.site_lng)
        mo.altitude, mo.azimuth = round(alt, 3), round(az, 3)
        mo.time_to_meridian_h = round(astro.time_to_meridian_hours(mo.ra_hours, mo.site_lng), 3)
        mo.side_of_pier = "west" if mo.time_to_meridian_h > 0 else "east"

    def _tick_focuser(self, dt: float) -> None:
        f = self.focuser
        if not f.connected:
            return
        # 最佳焦点随温度漂移(每降温 1°C 内移 ~12 步)
        self._best_focus = int(14300 + (self._best_focus_ref_temp - f.temperature) * 12)
        speed = 1200 * dt
        if f.position != self._focus_target:
            d = self._focus_target - f.position
            if abs(d) <= speed:
                f.position = self._focus_target
                f.is_moving = False
            else:
                f.position += int(math.copysign(speed, d))
                f.is_moving = True
        else:
            f.is_moving = False
        # 温度缓慢下降(夜间)
        f.temperature += (10.0 - f.temperature) * 0.0005 * dt

    def _tick_filterwheel(self, dt: float) -> None:
        fw = self.filterwheel
        if not fw.connected:
            return
        if fw.position != self._fw_target:
            fw._move_acc = getattr(fw, "_move_acc", 0.0) + dt
            fw.is_moving = True
            if fw._move_acc >= 1.2:                  # 切换约 1.2s
                fw.position = self._fw_target
                fw.is_moving = False
                fw._move_acc = 0.0
                cur = fw.filters[fw.position].name if fw.filters else ""
                self.bus.publish("FILTERWHEEL-CHANGED", domain="filterwheel",
                                 position=fw.position, name=cur)

    def _tick_cooler(self, dt: float) -> None:
        c = self.camera
        if not c.connected:
            return
        ambient = 18.0
        if c.cooler_on:
            target = c.target_temperature
            c.temperature += (target - c.temperature) * 0.08 * dt
            delta = max(0.0, ambient - c.temperature)
            c.cooler_power = round(min(100.0, 8 + delta * 3.2), 1)
        else:
            c.temperature += (ambient - c.temperature) * 0.05 * dt
            c.cooler_power = 0.0
        self._refresh_camera_controls()

    def _tick_guider(self, dt: float) -> None:
        g = self.guider
        if not g.connected or g.state not in ("guiding", "dithering"):
            return
        g._acc = getattr(g, "_acc", 0.0) + dt
        if g._acc < 1.0:                              # 约 1Hz 出一个修正点
            return
        g._acc = 0.0
        self._guide_t0 += 1.0
        amp = 0.9 if g.state == "guiding" else 3.0
        ra_raw = random.gauss(0, amp) + math.sin(self._guide_t0 / 25) * 0.3
        dec_raw = random.gauss(0, amp) + math.sin(self._guide_t0 / 40) * 0.25
        step = m.GuideStep(
            t=self._guide_t0,
            ra_raw=round(ra_raw, 3), dec_raw=round(dec_raw, 3),
            ra_dist=round(ra_raw * g.pixel_scale, 3), dec_dist=round(dec_raw * g.pixel_scale, 3),
            ra_duration=round(abs(ra_raw) * 40, 1), dec_duration=round(abs(dec_raw) * 40, 1))
        self._guide_steps.append(step)
        self._guide_steps = self._guide_steps[-240:]
        recent = self._guide_steps[-50:]
        g.rms_ra = round((sum(s.ra_dist ** 2 for s in recent) / len(recent)) ** 0.5, 3)
        g.rms_dec = round((sum(s.dec_dist ** 2 for s in recent) / len(recent)) ** 0.5, 3)
        g.rms_total = round((g.rms_ra ** 2 + g.rms_dec ** 2) ** 0.5, 3)
        # PHD2 式星点指标(模拟):随导星质量小幅抖动
        g.snr = round(max(3.0, 42 + random.uniform(-6, 6)), 1)
        g.star_mass = round(28000 + random.uniform(-3000, 3000), 0)
        g.hfd = round(2.6 + random.uniform(-0.2, 0.2), 2)
        g.avg_dist = round(math.hypot(ra_raw, dec_raw) * 0.7, 3)

    def _tick_weather(self, dt: float) -> None:
        w = self.weather
        if not w.connected:
            return
        w._acc = getattr(w, "_acc", 0.0) + dt
        if w._acc < 5.0:
            return
        w._acc = 0.0
        w.temperature = round(w.temperature + random.uniform(-0.2, 0.2), 1)
        w.humidity = round(min(100, max(0, w.humidity + random.uniform(-1, 1))), 1)
        w.cloud_cover = round(min(100, max(0, w.cloud_cover + random.uniform(-2, 2))), 1)
        w.wind_speed = round(max(0, w.wind_speed + random.uniform(-0.5, 0.5)), 1)
        self.safety.is_safe = w.cloud_cover < 70 and w.wind_speed < 12

    def _tick_dome(self, dt: float) -> None:
        d = self.dome
        if not d.connected:
            return
        if d.slewing:
            target = getattr(d, "_az_target", d.azimuth)
            diff = (target - d.azimuth + 540) % 360 - 180
            rate = 20 * dt
            if abs(diff) <= rate:
                d.azimuth = target % 360
                d.slewing = False
                self.bus.publish("DOME-SLEWED", domain="dome", azimuth=round(d.azimuth, 1))
            else:
                d.azimuth = (d.azimuth + math.copysign(rate, diff)) % 360
        elif d.following and self.mount.connected:
            d.azimuth = round(self.mount.azimuth, 1)

    # ====================================================================== #
    # 相机控制项
    # ====================================================================== #
    def _refresh_camera_controls(self) -> None:
        c = self.camera
        c.controls = [
            m.CameraControl(name="Gain", value=c.gain, display=str(c.gain), min=0, max=300),
            m.CameraControl(name="Offset", value=c.offset, display=str(c.offset), min=0, max=200),
            m.CameraControl(name="Temperature", value=round(c.temperature, 1),
                            display=f"{c.temperature:.1f}°C", min=-50, max=40, writable=False),
            m.CameraControl(name="TargetTemp", value=c.target_temperature,
                            display=f"{c.target_temperature:.0f}°C", min=-40, max=20),
            m.CameraControl(name="CoolerOn", value=1 if c.cooler_on else 0,
                            display="On" if c.cooler_on else "Off", min=0, max=1),
            m.CameraControl(name="CoolPower", value=c.cooler_power,
                            display=f"{c.cooler_power:.0f}%", min=0, max=100, writable=False),
        ]

    def _focus_error(self) -> float:
        return float(self.focuser.position - self._best_focus)

    # ====================================================================== #
    # 读取
    # ====================================================================== #
    async def get_camera(self) -> m.CameraState:
        return self.camera

    async def get_mount(self) -> m.MountState:
        return self.mount

    async def get_focuser(self) -> m.FocuserState:
        return self.focuser

    async def get_autofocus(self) -> m.AutoFocusResult:
        return self.autofocus

    async def get_filterwheel(self) -> m.FilterWheelState:
        return self.filterwheel

    async def get_guider(self) -> m.GuiderState:
        return self.guider

    async def get_guider_graph(self) -> list[m.GuideStep]:
        return self._guide_steps[-120:]

    async def get_guider_star_image(self) -> dict:
        import base64
        g = self.guider
        if not g.connected or g.state not in ("guiding", "dithering", "calibrating"):
            return {"available": False, "reason": "SIM:未在导星(开始导星后显示星点画面)"}
        size = max(15, int(self.s.phd2_star_size))
        last = self._guide_steps[-1] if self._guide_steps else None
        drift = (last.ra_raw if last else 0.0)
        img, sx, sy = imaging.render_guide_star(
            size, seed=int(self._guide_t0) & 0x7FFFFFFF,
            hfd=g.hfd or 2.6, snr=g.snr or 40.0, drift=drift)
        png = imaging.stretch_guide_png(img)
        return {
            "available": True,
            "frame": int(self._guide_t0),
            "width": size, "height": size,
            "star": [sx, sy],
            "image": "data:image/png;base64," + base64.b64encode(png).decode("ascii"),
        }

    async def get_rotator(self) -> m.RotatorState:
        return self.rotator

    async def get_dome(self) -> m.DomeState:
        return self.dome

    async def get_flatdevice(self) -> m.FlatDeviceState:
        return self.flat

    async def get_switch(self) -> m.SwitchState:
        return self.switch

    async def get_weather(self) -> m.WeatherState:
        return self.weather

    async def get_safety(self) -> m.SafetyState:
        return self.safety

    async def get_sequence(self) -> m.SequenceState:
        return self.sequence

    async def get_framing(self) -> m.FramingState:
        return self.framing

    # ====================================================================== #
    # 相机动作
    # ====================================================================== #
    async def camera_action(self, action: str, p: dict) -> dict:
        c = self.camera
        if not c.connected and action != "connect":
            return {"ok": False, "error": "相机未连接"}
        if action == "set_control":
            name = p.get("name")
            val = p.get("value")
            if name == "Gain":
                c.gain = int(val)
            elif name == "Offset":
                c.offset = int(val)
            elif name == "TargetTemp":
                c.target_temperature = float(val)
            elif name == "CoolerOn":
                c.cooler_on = bool(int(val))
            elif name in ("exposure", "Exposure"):
                c.exposure_s = float(val)
            elif name in ("bin", "Bin"):
                c.bin = int(val)
            elif name == "DewHeater":
                c.dew_heater_on = bool(int(val))
            else:
                return {"ok": False, "error": f"未知控制项 {name}"}
            self._refresh_camera_controls()
            self.bus.state_changed("camera")
            return {"ok": True}
        if action == "cool":
            c.target_temperature = float(p.get("temperature", c.target_temperature))
            c.cooler_on = True
            return {"ok": True}
        if action == "warm":
            c.cooler_on = False
            c.target_temperature = 15.0
            return {"ok": True}
        if action == "capture":
            if c.is_exposing or (self._seq_task and not self._seq_task.done()):
                return {"ok": False, "error": "正在曝光/序列运行中"}
            exposure = float(p.get("exposure", c.exposure_s))
            gain = int(p.get("gain", c.gain))
            binning = int(p.get("bin", c.bin))
            mode = p.get("mode", "single")
            filt = self._current_filter()
            c.exposure_s, c.gain, c.bin, c.capture_mode = exposure, gain, binning, mode
            self._abort_capture = False
            self._capture_task = self._spawn(self._capture_loop(exposure, gain, binning, filt, mode))
            return {"ok": True, "mode": mode}
        if action == "abort":
            self._abort_capture = True
            self.camera.capture_mode = "single"
            return {"ok": True}
        if action == "set_subframe":
            c.subframe = {k: int(p.get(k, c.subframe.get(k, 0)))
                          for k in ("x", "y", "width", "height")}
            return {"ok": True}
        return {"ok": False, "error": f"未知相机动作 {action}"}

    def _current_filter(self) -> str:
        fw = self.filterwheel
        if fw.connected and fw.filters:
            return fw.filters[fw.position].name
        return "—"

    async def _capture_loop(self, exposure, gain, binning, filt, mode) -> None:
        while True:
            await self._expose(exposure, gain, binning, filt, "LIGHT", self.mount.target_name or "天空")
            if self._abort_capture or mode == "single":
                break
            await asyncio.sleep(0.3)
        self.camera.capture_mode = "single"

    async def _expose(self, exposure_s, gain, binning, filt, ftype, target) -> m.ImageMeta:
        c = self.camera
        c.is_exposing = True
        c.state = "exposing"
        c.exposure_total_s = exposure_s
        c.exposure_elapsed_s = 0.0
        c.progress = 0.0
        self.bus.publish("CAMERA-CAPTURE-START", domain="camera",
                         exposure=exposure_s, filter=filt, target=target)
        steps = max(1, int(exposure_s / 0.2))
        for i in range(steps):
            if self._abort_capture:
                break
            await asyncio.sleep(exposure_s / steps)
            c.exposure_elapsed_s = round((i + 1) * exposure_s / steps, 2)
            c.progress = round(c.exposure_elapsed_s / exposure_s, 3)
        c.state = "downloading"
        await asyncio.sleep(0.05)
        meta = self._render_and_store(exposure_s, gain, binning, filt, ftype, target)
        c.is_exposing = False
        c.state = "idle"
        c.progress = 1.0
        c.last_image_id = meta.image_id
        self.bus.publish("IMAGE-SAVE", domain="camera", image_id=meta.image_id,
                         hfr=meta.hfr, stars=meta.stars, filter=filt,
                         exposure=exposure_s, target=target)
        return meta

    def _render_and_store(self, exposure_s, gain, binning, filt, ftype, target) -> m.ImageMeta:
        self._image_counter += 1
        iid = self._image_counter
        seed = (hash(target) ^ (iid * 2654435761)) & 0x7FFFFFFF
        fe, grms, temp = self._focus_error(), self.guider.rms_total, self.camera.temperature
        self._images[iid] = dict(seed=seed, exposure_s=exposure_s, gain=gain, bin=binning,
                                 filter=filt, target=target, fe=fe, grms=grms, temp=temp)
        # 控制内存:只保留最近 60 张可重渲染参数
        if len(self._images) > 60:
            oldest = min(self._images)
            self._images.pop(oldest, None)
        hfr, stars = imaging.estimate_hfr_stars(fe, grms, exposure_s, gain)
        arr = self._render(iid)
        meta = m.ImageMeta(
            image_id=iid, width=imaging.PREVIEW_W, height=imaging.PREVIEW_H,
            exposure_s=exposure_s, gain=gain, offset=self.camera.offset, bin=binning,
            filter=filt, temperature=round(temp, 1), mean=round(float(arr.mean()), 1),
            hfr=hfr, stars=stars, captured_at=_now(), target=target,
            image_url=f"/api/camera/image?image_id={iid}",
            histogram=imaging.histogram(arr))
        self._metas.append(meta)
        self._metas = self._metas[-500:]
        return meta

    def _render(self, iid: int):
        p = self._images.get(iid)
        if p is None:
            return imaging.render_frame(iid, 1.0, 100, 0, "", 0, -10)
        return imaging.render_frame(p["seed"], p["exposure_s"], p["gain"], p["fe"],
                                    p["filter"] + " " + p["target"], p["grms"], p["temp"])

    # ====================================================================== #
    # 影像
    # ====================================================================== #
    async def get_image_meta(self, image_id=None) -> m.ImageMeta | None:
        if image_id is None:
            return self._metas[-1] if self._metas else None
        for mm in reversed(self._metas):
            if mm.image_id == image_id:
                return mm
        return None

    async def get_image_png(self, image_id=None, stretch=True) -> bytes | None:
        if image_id is None:
            image_id = self._image_counter
        if image_id not in self._images and image_id != self._image_counter:
            # 可能已被淘汰
            if not self._metas:
                return None
        arr = self._render(image_id)
        return imaging.stretch_to_png(arr)

    # ====================================================================== #
    # 赤道仪动作
    # ====================================================================== #
    async def mount_action(self, action: str, p: dict) -> dict:
        mo = self.mount
        if not mo.connected:
            return {"ok": False, "error": "赤道仪未连接"}
        if action == "slew":
            mo.target_ra_hours = float(p["ra"])
            mo.target_dec_degrees = float(p["dec"])
            mo.target_name = p.get("target_name", mo.target_name)
            mo.at_park = False
            mo.tracking = True
            mo.slewing = True
            self._slew_target = (mo.target_ra_hours, mo.target_dec_degrees)
            self.bus.publish("MOUNT-SLEW-START", domain="mount", target=mo.target_name)
            return {"ok": True}
        if action == "sync":
            mo.ra_hours = float(p["ra"])
            mo.dec_degrees = float(p["dec"])
            self._recompute_mount()
            return {"ok": True}
        if action == "stop":
            mo.slewing = False
            self._slew_target = None
            return {"ok": True}
        if action == "set_tracking":
            mo.tracking = bool(p.get("on", True))
            self._mount_altaz_hold = None
            mo.at_park = False
            self.bus.state_changed("mount")
            return {"ok": True}
        if action == "set_tracking_mode":
            mo.tracking_mode = p.get("mode", "sidereal")
            return {"ok": True}
        if action == "park":
            mo.tracking = False
            mo.slewing = True
            self._slew_target = (astro.lst_hours(mo.site_lng), 89.9)
            mo.target_name = "停靠位"
            self._spawn(self._after_park())
            return {"ok": True}
        if action == "unpark":
            mo.at_park = False
            return {"ok": True}
        if action == "home":
            mo.slewing = True
            mo.tracking = False
            self._slew_target = (astro.lst_hours(mo.site_lng), 89.9)
            mo.at_home = True
            return {"ok": True}
        if action == "flip":
            self.bus.publish("MOUNT-BEFORE-FLIP", domain="mount")
            mo.slewing = True
            self._slew_target = (mo.ra_hours, mo.dec_degrees)
            self._spawn(self._after_flip())
            return {"ok": True}
        if action == "move_axis":
            # 手动微动:即时改变指向一点点
            d = p.get("direction", "n")
            rate = float(p.get("rate", 0.5))
            if d == "n":
                mo.dec_degrees += rate
            elif d == "s":
                mo.dec_degrees -= rate
            elif d == "e":
                mo.ra_hours += rate / 15.0
            elif d == "w":
                mo.ra_hours -= rate / 15.0
            self._recompute_mount()
            return {"ok": True}
        return {"ok": False, "error": f"未知赤道仪动作 {action}"}

    async def _after_park(self) -> None:
        await self._wait_slew()
        self.mount.at_park = True
        self.bus.publish("MOUNT-PARKED", domain="mount")

    async def _after_flip(self) -> None:
        await self._wait_slew()
        self.mount.side_of_pier = "east" if self.mount.side_of_pier == "west" else "west"
        self.bus.publish("MOUNT-AFTER-FLIP", domain="mount")

    async def _wait_slew(self, timeout=30.0) -> None:
        t = 0.0
        while self.mount.slewing and t < timeout:
            await asyncio.sleep(0.1)
            t += 0.1

    # ====================================================================== #
    # 调焦 + 自动对焦
    # ====================================================================== #
    async def focuser_action(self, action: str, p: dict) -> dict:
        f = self.focuser
        if not f.connected:
            return {"ok": False, "error": "调焦未连接"}
        if action == "move_abs":
            self._focus_target = max(0, min(f.max_step, int(p["position"])))
            return {"ok": True}
        if action == "move_rel":
            self._focus_target = max(0, min(f.max_step, f.position + int(p["steps"])))
            return {"ok": True}
        if action == "halt":
            self._focus_target = f.position
            return {"ok": True}
        if action == "set_tempcomp":
            f.temp_comp = bool(p.get("on", False))
            return {"ok": True}
        if action == "autofocus_start":
            if f.af_running:
                return {"ok": False, "error": "自动对焦进行中"}
            self._af_task = self._spawn(self._autofocus())
            return {"ok": True}
        if action == "autofocus_cancel":
            self.autofocus.running = False
            return {"ok": True}
        return {"ok": False, "error": f"未知调焦动作 {action}"}

    async def _autofocus(self) -> m.AutoFocusResult:
        f = self.focuser
        af = self.autofocus
        f.af_running = True
        af.running = True
        af.points = []
        af.error = ""
        af.finished_at = ""
        self.bus.publish("AUTOFOCUS-STARTING", domain="focuser")
        center = self._best_focus + random.randint(-400, 400)
        span, n = 1800, 9
        positions = [int(center - span + 2 * span * i / (n - 1)) for i in range(n)]
        for pos in positions:
            if not af.running:
                break
            self._focus_target = pos
            await self._wait_focuser()
            await asyncio.sleep(0.15)
            # 近焦 HFR 呈二次关系(便于抛物线拟合,贴近真实 V 曲线底部)
            fe = self._focus_error()
            hfr = round(1.5 + (fe / 650.0) ** 2 + random.uniform(-0.04, 0.04), 3)
            af.points.append(m.AutoFocusPoint(position=pos, hfr=hfr))
            self.bus.publish("AUTOFOCUS-POINT-ADDED", domain="focuser",
                             position=pos, hfr=round(hfr, 3))
        # 抛物线拟合 hfr = a x^2 + b x + c
        if len(af.points) >= 3:
            xs = [pt.position for pt in af.points]
            ys = [pt.hfr for pt in af.points]
            a, b, c = _parabola_fit(xs, ys)
            af.fitting = [a, b, c]
            if a > 0:
                best = int(-b / (2 * a))
                af.best_position = max(0, min(f.max_step, best))
                af.best_hfr = round(a * best * best + b * best + c, 3)
                af.r_squared = round(_r_squared(xs, ys, a, b, c), 4)
                self._focus_target = af.best_position
                await self._wait_focuser()
        af.running = False
        af.finished_at = _now()
        f.af_running = False
        self.bus.publish("AUTOFOCUS-FINISHED", domain="focuser",
                         best_position=af.best_position, best_hfr=af.best_hfr)
        return af

    async def _wait_focuser(self, timeout=15.0) -> None:
        t = 0.0
        while self.focuser.is_moving and t < timeout:
            await asyncio.sleep(0.05)
            t += 0.05
        # 等到位
        while self.focuser.position != self._focus_target and t < timeout:
            await asyncio.sleep(0.05)
            t += 0.05

    # ====================================================================== #
    # 滤镜轮
    # ====================================================================== #
    async def filterwheel_action(self, action: str, p: dict) -> dict:
        fw = self.filterwheel
        if not fw.connected:
            return {"ok": False, "error": "滤镜轮未连接"}
        if action == "change":
            if "position" in p:
                pos = int(p["position"])
            else:
                name = p.get("name", "")
                pos = next((s.position for s in fw.filters if s.name == name), fw.position)
            self._fw_target = max(0, min(len(fw.filters) - 1, pos))
            return {"ok": True, "target": self._fw_target}
        if action == "set_names":
            for i, name in enumerate(p.get("names", [])):
                if i < len(fw.filters):
                    fw.filters[i].name = name
            return {"ok": True}
        return {"ok": False, "error": f"未知滤镜轮动作 {action}"}

    async def _wait_filterwheel(self, timeout=8.0) -> None:
        t = 0.0
        while self.filterwheel.is_moving and t < timeout:
            await asyncio.sleep(0.05)
            t += 0.05
        while self.filterwheel.position != self._fw_target and t < timeout:
            await asyncio.sleep(0.05)
            t += 0.05

    # ====================================================================== #
    # 导星
    # ====================================================================== #
    async def guider_action(self, action: str, p: dict) -> dict:
        g = self.guider
        if not g.connected:
            return {"ok": False, "error": "导星未连接"}
        if action == "start":
            g.state = "calibrating"
            self._spawn(self._guider_calibrate())
            return {"ok": True}
        if action == "stop":
            g.state = "idle"
            self.bus.publish("GUIDER-STOP", domain="guider")
            return {"ok": True}
        if action == "dither":
            if g.state == "guiding":
                g.state = "dithering"
                g.settling = True
                self._spawn(self._guider_settle())
                self.bus.publish("GUIDER-DITHER", domain="guider")
            return {"ok": True}
        if action == "clear_calibration":
            self.bus.publish("GUIDER-CLEAR-CALIBRATION", domain="guider")
            return {"ok": True}
        if action == "auto_select":
            return {"ok": True, "star": {"x": 512, "y": 341}}
        return {"ok": False, "error": f"未知导星动作 {action}"}

    async def _guider_calibrate(self) -> None:
        await asyncio.sleep(2.0)
        if self.guider.state == "calibrating":
            self.guider.state = "guiding"
            self.bus.publish("GUIDER-START", domain="guider")

    async def _guider_settle(self) -> None:
        await asyncio.sleep(4.0)
        if self.guider.state == "dithering":
            self.guider.state = "guiding"
            self.guider.settling = False

    # ====================================================================== #
    # 旋转器
    # ====================================================================== #
    async def rotator_action(self, action: str, p: dict) -> dict:
        r = self.rotator
        if not r.connected:
            return {"ok": False, "error": "旋转器未连接"}
        if action in ("move", "move_mechanical"):
            r.position = float(p.get("position", r.position)) % 360
            r.mechanical_position = r.position
            self.bus.publish("ROTATOR-MOVED", domain="rotator", position=r.position)
            return {"ok": True}
        if action == "sync":
            r.position = float(p.get("position", r.position)) % 360
            return {"ok": True}
        if action == "halt":
            r.is_moving = False
            return {"ok": True}
        return {"ok": False, "error": f"未知旋转器动作 {action}"}

    # ====================================================================== #
    # 圆顶 / 平场 / 开关
    # ====================================================================== #
    async def dome_action(self, action: str, p: dict) -> dict:
        d = self.dome
        if not d.connected:
            return {"ok": False, "error": "圆顶未连接"}
        if action == "open_shutter":
            d.shutter = "opening"
            self._spawn(self._dome_shutter("open"))
            return {"ok": True}
        if action == "close_shutter":
            d.shutter = "closing"
            self._spawn(self._dome_shutter("closed"))
            return {"ok": True}
        if action == "slew":
            d._az_target = float(p.get("azimuth", d.azimuth)) % 360
            d.slewing = True
            return {"ok": True}
        if action == "park":
            d._az_target = 90.0
            d.slewing = True
            self._spawn(self._dome_after_park())
            return {"ok": True}
        if action == "find_home":
            d._az_target = 0.0
            d.slewing = True
            return {"ok": True}
        if action == "set_follow":
            d.following = bool(p.get("on", False))
            return {"ok": True}
        if action == "stop":
            d.slewing = False
            return {"ok": True}
        return {"ok": False, "error": f"未知圆顶动作 {action}"}

    async def _dome_shutter(self, target: str) -> None:
        await asyncio.sleep(2.0)
        self.dome.shutter = target
        self.bus.publish("DOME-SHUTTER-" + ("OPENED" if target == "open" else "CLOSED"),
                         domain="dome")

    async def _dome_after_park(self) -> None:
        while self.dome.slewing:
            await asyncio.sleep(0.1)
        self.dome.at_park = True
        self.bus.publish("DOME-PARKED", domain="dome")

    async def flatdevice_action(self, action: str, p: dict) -> dict:
        f = self.flat
        if not f.connected:
            return {"ok": False, "error": "平场未连接"}
        if action == "open_cover":
            self._spawn(self._flat_cover("open"))
            return {"ok": True}
        if action == "close_cover":
            self._spawn(self._flat_cover("closed"))
            return {"ok": True}
        if action == "set_light":
            f.light_on = bool(p.get("on", False))
            self.bus.publish("FLAT-LIGHT-TOGGLED", domain="flatdevice", on=f.light_on)
            return {"ok": True}
        if action == "set_brightness":
            f.brightness = max(0, min(f.max_brightness, int(p.get("brightness", f.brightness))))
            return {"ok": True}
        return {"ok": False, "error": f"未知平场动作 {action}"}

    async def _flat_cover(self, target: str) -> None:
        self.flat.cover = "opening" if target == "open" else "closing"
        await asyncio.sleep(1.5)
        self.flat.cover = target
        self.bus.publish("FLAT-COVER-" + ("OPENED" if target == "open" else "CLOSED"),
                         domain="flatdevice")

    async def switch_action(self, action: str, p: dict) -> dict:
        sw = self.switch
        if not sw.connected:
            return {"ok": False, "error": "开关未连接"}
        if action == "set":
            sid = int(p.get("id", -1))
            val = float(p.get("value", 0))
            for s in sw.switches:
                if s.id == sid and s.writable:
                    s.value = max(s.min, min(s.max, val))
                    self.bus.publish("SWITCH-CHANGED", domain="switch", id=sid, value=s.value)
                    return {"ok": True}
            return {"ok": False, "error": "开关不存在或只读"}
        return {"ok": False, "error": f"未知开关动作 {action}"}

    # ====================================================================== #
    # 板解算 / 天文台条件
    # ====================================================================== #
    async def platesolve(self) -> m.PlateSolveResult:
        if not self._metas:
            return m.PlateSolveResult(ok=False, error="无图像可解算 —— 请先拍一张")
        mo = self.mount
        if mo.connected:
            ra = (mo.ra_hours + random.uniform(-0.008, 0.008)) % 24
            dec = max(-90, min(90, mo.dec_degrees + random.uniform(-0.08, 0.08)))
        else:
            ra, dec = self.framing.ra_hours, self.framing.dec_degrees
        res = m.PlateSolveResult(
            ok=True, solved=True, ra_hours=round(ra, 5), dec_degrees=round(dec, 4),
            ra_text=astro.hours_to_hms(ra), dec_text=astro.deg_to_dms(dec),
            rotation=round(self.framing.rotation, 1))
        self.bus.publish("PLATESOLVE-FINISHED", domain="camera",
                         ra=res.ra_hours, dec=res.dec_degrees)
        return res

    async def get_conditions(self) -> dict:
        sun = astro.sun_altitude(self.mount.site_lat, self.mount.site_lng)
        if sun >= 0:
            tw = "白天"
        elif sun >= -6:
            tw = "民用暮光"
        elif sun >= -12:
            tw = "航海暮光"
        elif sun >= -18:
            tw = "天文暮光"
        else:
            tw = "天文夜"
        return {"ok": True, "sun_altitude": round(sun, 2), "twilight": tw,
                "is_safe": self.safety.is_safe if self.safety.connected else None}

    # ====================================================================== #
    # 构图
    # ====================================================================== #
    async def framing_search(self, q: str) -> list[m.FramingTarget]:
        q = (q or "").strip().lower()
        out = []
        for o in CATALOG:
            if not q or q in o["name"].lower() or q in o["cat"].lower() or q in o["type"]:
                out.append(m.FramingTarget(
                    name=o["name"], type=o["type"], ra_hours=o["ra"], dec_degrees=o["dec"],
                    magnitude=o["mag"], size_arcmin=o["size"], catalog=o["cat"]))
        return out

    async def framing_action(self, action: str, p: dict) -> dict:
        fr = self.framing
        if action == "set_coordinates":
            fr.target_name = p.get("target_name", "")
            fr.ra_hours = float(p.get("ra", fr.ra_hours))
            fr.dec_degrees = float(p.get("dec", fr.dec_degrees))
            return {"ok": True}
        if action == "set_rotation":
            fr.rotation = float(p.get("rotation", 0)) % 360
            return {"ok": True}
        if action == "slew_center":
            await self.mount_action("slew", {"ra": fr.ra_hours, "dec": fr.dec_degrees,
                                             "target_name": fr.target_name})
            return {"ok": True}
        return {"ok": False, "error": f"未知构图动作 {action}"}

    # ====================================================================== #
    # 图像库
    # ====================================================================== #
    async def library_summary(self) -> dict:
        by_filter: dict[str, int] = {}
        by_target: dict[str, int] = {}
        total_exp = 0.0
        for mm in self._metas:
            by_filter[mm.filter] = by_filter.get(mm.filter, 0) + 1
            by_target[mm.target] = by_target.get(mm.target, 0) + 1
            total_exp += mm.exposure_s
        return {"ok": True, "total": len(self._metas), "by_filter": by_filter,
                "by_target": by_target, "total_integration_s": round(total_exp, 1)}

    async def library_list(self, **filters) -> list[m.ImageMeta]:
        items = list(reversed(self._metas))
        tgt = filters.get("target")
        flt = filters.get("filter")
        if tgt:
            items = [i for i in items if i.target == tgt]
        if flt:
            items = [i for i in items if i.filter == flt]
        return items[: int(filters.get("limit", 200))]

    def library_thumb(self, image_id: int) -> bytes | None:
        if image_id not in self._images:
            return None
        return imaging.thumbnail_png(self._render(image_id))

    # ====================================================================== #
    # 序列执行
    # ====================================================================== #
    async def sequence_action(self, action: str, p: dict) -> dict:
        seq = self.sequence
        if action == "set_plan":
            seq.plan = m.SequencePlan(**p["plan"])
            seq.status = "idle"
            return {"ok": True}
        if action == "start":
            if seq.running:
                return {"ok": False, "error": "序列已在运行"}
            if not seq.plan.targets:
                return {"ok": False, "error": "计划为空"}
            self._seq_stop = False
            self._seq_task = self._spawn(self._run_sequence())
            return {"ok": True}
        if action == "stop":
            self._seq_stop = True
            seq.status = "idle"
            return {"ok": True}
        if action == "pause":
            if seq.running:
                seq.status = "paused"
            return {"ok": True}
        if action == "resume":
            if seq.status == "paused":
                seq.status = "running"
            return {"ok": True}
        if action == "reset":
            for t in seq.plan.targets:
                for e in t.exposures:
                    e.completed = 0
            seq.completed_frames = 0
            seq.rejected_frames = 0
            seq.current_loop = 0
            seq.progress = 0.0
            seq.status = "idle"
            return {"ok": True}
        return {"ok": False, "error": f"未知序列动作 {action}"}

    def _seq_log(self, msg: str) -> None:
        self.sequence.log.append(f"{datetime.now().strftime('%H:%M:%S')} {msg}")
        self.sequence.log = self.sequence.log[-100:]

    async def _wait_for_twilight(self, threshold: float) -> None:
        """暮光起始门:等到日高度 ≤ threshold 才返回(可被停止打断)。"""
        sun = astro.sun_altitude(self.mount.site_lat, self.mount.site_lng)
        if sun <= threshold:
            return
        self.sequence.status = "waiting"
        self._seq_log(f"等待天黑:当前日高度 {sun:.1f}°,需 ≤ {threshold}°")
        while not self._seq_stop:
            sun = astro.sun_altitude(self.mount.site_lat, self.mount.site_lng)
            if sun <= threshold:
                self.sequence.status = "running"
                self._seq_log("已入暮光,开始拍摄")
                return
            self.sequence.current_action = f"等待天黑 · 日高度 {sun:.1f}°"
            await asyncio.sleep(5.0)

    def _should_stop_for_dawn(self, plan) -> bool:
        if plan.stop_at_sun_altitude is None:
            return False
        return astro.sun_altitude(self.mount.site_lat, self.mount.site_lng) >= plan.stop_at_sun_altitude

    async def _run_sequence(self) -> None:
        seq = self.sequence
        plan = seq.plan
        seq.status = "running"
        seq.running = True
        seq.started_at = _now()
        seq.log = []
        loops = max(1, plan.loop_count)
        frames_per_loop = sum(e.count for t in plan.targets for e in t.exposures)
        seq.total_frames = frames_per_loop * loops
        seq.completed_frames = 0
        seq.rejected_frames = 0
        seq.current_loop = 0
        avg_exp = (sum(e.exposure_s * e.count for t in plan.targets for e in t.exposures)
                   / max(1, frames_per_loop))
        self.bus.publish("SEQUENCE-STARTING", domain="sequence", name=plan.name)
        self._seq_log(f"序列开始:{plan.name}" + (f" · 重复 {loops} 轮" if loops > 1 else ""))
        try:
            if plan.start_at_sun_altitude is not None:
                await self._wait_for_twilight(plan.start_at_sun_altitude)
            if plan.cool_camera_to is not None and self.camera.connected:
                self._seq_log(f"制冷至 {plan.cool_camera_to}°C")
                await self.camera_action("cool", {"temperature": plan.cool_camera_to})

            for loop_i in range(loops):
                if self._seq_stop:
                    break
                seq.current_loop = loop_i + 1
                if loops > 1:
                    self._seq_log(f"———— 第 {loop_i + 1}/{loops} 轮 ————")
                for t in plan.targets:           # 每轮各行计数从 0 起
                    for e in t.exposures:
                        e.completed = 0

                for ti, tgt in enumerate(plan.targets):
                    if self._seq_stop:
                        break
                    seq.current_target_index = ti
                    seq.current_action = f"GOTO {tgt.name}"
                    self._seq_log(f"目标 {tgt.name}:转向")
                    if self.mount.connected:
                        await self.mount_action("slew", {"ra": tgt.ra_hours, "dec": tgt.dec_degrees,
                                                         "target_name": tgt.name})
                        await self._wait_slew()
                    if self.guider.connected and self.guider.state != "guiding":
                        await self.guider_action("start", {})
                        self._seq_log("启动导星")
                        await asyncio.sleep(2.5)
                    last_temp = self.focuser.temperature
                    for ei, exp in enumerate(tgt.exposures):
                        if self._seq_stop:
                            break
                        seq.current_exposure_index = ei
                        if self.filterwheel.connected:
                            await self.filterwheel_action("change", {"name": exp.filter})
                            await self._wait_filterwheel()
                        if tgt.autofocus_on_filter_change and self.focuser.connected:
                            self._seq_log(f"滤镜 {exp.filter}:自动对焦")
                            await self._autofocus()
                            last_temp = self.focuser.temperature
                        while exp.completed < exp.count:
                            if self._seq_stop:
                                break
                            while seq.status == "paused" and not self._seq_stop:
                                await asyncio.sleep(0.3)
                            # 安全监视中止
                            if (plan.abort_on_unsafe and self.safety.connected
                                    and not self.safety.is_safe):
                                self._seq_log("安全监视器报警 —— 中止序列")
                                self.bus.publish("SEQUENCE-ABORT-UNSAFE", domain="sequence")
                                self._seq_stop = True
                                break
                            # 暮光停止(天亮)
                            if self._should_stop_for_dawn(plan):
                                self._seq_log("日出在即 —— 停止序列")
                                self._seq_stop = True
                                break
                            # 温变自动对焦
                            if (tgt.autofocus_on_temp_delta and self.focuser.connected
                                    and abs(self.focuser.temperature - last_temp) >= tgt.autofocus_on_temp_delta):
                                self._seq_log("温变触发自动对焦")
                                await self._autofocus()
                                last_temp = self.focuser.temperature
                            # 子午翻转
                            if (tgt.meridian_flip and self.mount.connected
                                    and self.mount.time_to_meridian_h is not None
                                    and -0.05 < self.mount.time_to_meridian_h <= 0):
                                self._seq_log("子午翻转")
                                await self.mount_action("flip", {})
                                await self._wait_slew()
                            seq.current_action = (f"{tgt.name} · {exp.filter} · {exp.exposure_s:.0f}s "
                                                  f"({exp.completed + 1}/{exp.count})"
                                                  + (f" · 第{seq.current_loop}轮" if loops > 1 else ""))
                            meta = await self._expose(exp.exposure_s, exp.gain, exp.bin,
                                                      exp.filter, exp.type, tgt.name)
                            exp.completed += 1
                            seq.completed_frames += 1
                            # 质量拒帧
                            if plan.reject_hfr_over and meta.hfr > plan.reject_hfr_over:
                                seq.rejected_frames += 1
                                self._seq_log(f"废帧:HFR {meta.hfr} > 限 {plan.reject_hfr_over}")
                                self.bus.publish("IMAGE-REJECTED", domain="sequence",
                                                 hfr=meta.hfr, image_id=meta.image_id)
                            seq.progress = round(seq.completed_frames / max(1, seq.total_frames), 3)
                            seq.estimated_remaining_s = round(
                                (seq.total_frames - seq.completed_frames) * (avg_exp + 2), 0)
                            if (tgt.dither_every and self.guider.state == "guiding"
                                    and exp.completed % tgt.dither_every == 0):
                                await self.guider_action("dither", {})
                                await asyncio.sleep(4.0)
            seq.status = "idle" if self._seq_stop else "finished"
            suffix = f",废帧 {seq.rejected_frames}" if seq.rejected_frames else ""
            self._seq_log(("序列已停止" if self._seq_stop else "序列结束") + suffix)
        except Exception as e:
            seq.status = "error"
            self._seq_log(f"错误:{e}")
            self.bus.publish("SEQUENCE-ENTITY-FAILED", domain="sequence", error=str(e))
        finally:
            seq.running = False
            seq.current_action = ""
            if not self._seq_stop:
                if plan.warm_at_end and self.camera.connected:
                    await self.camera_action("warm", {})
                if plan.park_at_end and self.mount.connected:
                    await self.mount_action("park", {})
            self.bus.publish("SEQUENCE-FINISHED", domain="sequence",
                             completed=seq.completed_frames, total=seq.total_frames,
                             rejected=seq.rejected_frames)


# --------------------------------------------------------------------------- #
# 数值工具
# --------------------------------------------------------------------------- #
def _parabola_fit(xs, ys):
    """最小二乘拟合二次多项式,返回原坐标系下的 (a,b,c)。

    焦点位置数值很大(~14000),直接用正规方程会病态;先把 x 中心化再拟合,
    最后把系数变换回原坐标,既稳定又保持前端可直接 a x^2+b x+c 求值。
    """
    n = len(xs)
    x0 = sum(xs) / n
    cx = [x - x0 for x in xs]
    sx = sum(cx); sx2 = sum(x * x for x in cx); sx3 = sum(x ** 3 for x in cx)
    sx4 = sum(x ** 4 for x in cx)
    sy = sum(ys); sxy = sum(x * y for x, y in zip(cx, ys))
    sx2y = sum(x * x * y for x, y in zip(cx, ys))
    A = [[sx4, sx3, sx2], [sx3, sx2, sx], [sx2, sx, n]]
    B = [sx2y, sxy, sy]
    ca, cb, cc = _solve3(A, B)               # 中心化坐标系下的系数
    # 变换回原坐标:y = ca(x-x0)^2 + cb(x-x0) + cc
    a = ca
    b = cb - 2 * ca * x0
    c = ca * x0 * x0 - cb * x0 + cc
    return a, b, c


def _solve3(A, B):
    import copy
    a = copy.deepcopy(A)
    b = list(B)
    for i in range(3):
        piv = a[i][i] or 1e-9
        for j in range(i + 1, 3):
            f = a[j][i] / piv
            for k in range(3):
                a[j][k] -= f * a[i][k]
            b[j] -= f * b[i]
    x = [0, 0, 0]
    for i in (2, 1, 0):
        s = b[i] - sum(a[i][k] * x[k] for k in range(i + 1, 3))
        x[i] = s / (a[i][i] or 1e-9)
    return x


def _r_squared(xs, ys, a, b, c):
    mean = sum(ys) / len(ys)
    ss_tot = sum((y - mean) ** 2 for y in ys) or 1e-9
    ss_res = sum((y - (a * x * x + b * x + c)) ** 2 for x, y in zip(xs, ys))
    return 1 - ss_res / ss_tot
