"""领域模型 —— 前端 /api/* 契约的单一事实来源。

这些模型与具体后端(模拟引擎 / 真实 NINA)无关:Sim 与 Live 两个 Gateway
都产出同样的模型,前端因此只认一套字段。字段命名尽量贴近 NINA Advanced API
(reference/ninaAPI) 与 NINA.Equipment 的语义,便于将来对接真机。
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


# --------------------------------------------------------------------------- #
# 通用
# --------------------------------------------------------------------------- #
class DeviceType(str, Enum):
    CAMERA = "camera"
    MOUNT = "mount"
    FOCUSER = "focuser"
    FILTERWHEEL = "filterwheel"
    GUIDER = "guider"
    ROTATOR = "rotator"
    DOME = "dome"
    FLAT = "flatdevice"
    SWITCH = "switch"
    WEATHER = "weather"
    SAFETY = "safetymonitor"


ALL_DEVICE_TYPES: list[str] = [d.value for d in DeviceType]


class DriverDescriptor(BaseModel):
    """一个可供选择的驱动(模拟器/ASCOM/Alpaca/原生)。"""
    id: str
    name: str
    category: str = "Simulator"          # Simulator | ASCOM | Alpaca | Native
    description: str = ""


class DeviceSummary(BaseModel):
    """设备页与总览页用的精简状态。"""
    type: str
    connected: bool = False
    name: str = ""
    driver_id: str = ""
    state: str = "disconnected"          # 自由文本: idle/slewing/exposing/...
    detail: str = ""


# --------------------------------------------------------------------------- #
# 相机
# --------------------------------------------------------------------------- #
class CameraControl(BaseModel):
    """一个可读可写的相机控制项(对应 NINA 的 gain/offset/temp 等)。"""
    name: str
    value: float
    display: str = ""
    min: Optional[float] = None
    max: Optional[float] = None
    step: Optional[float] = None
    writable: bool = True


class CameraState(BaseModel):
    connected: bool = False
    name: str = ""
    # 传感器
    sensor_name: str = ""
    is_color: bool = False
    bit_depth: int = 16
    chip_width: int = 0
    chip_height: int = 0
    pixel_size_um: float = 0.0
    bins: list[int] = Field(default_factory=lambda: [1, 2, 3, 4])
    has_cooler: bool = False
    can_subframe: bool = True
    # 当前曝光设置
    exposure_s: float = 1.0
    gain: int = 0
    offset: int = 0
    bin: int = 1
    readout_mode: int = 0
    readout_modes: list[str] = Field(default_factory=lambda: ["Normal"])
    subframe: dict[str, int] = Field(default_factory=dict)   # x,y,width,height
    # 制冷
    temperature: float = 20.0
    target_temperature: float = 0.0
    cooler_on: bool = False
    cooler_power: float = 0.0
    dew_heater_on: bool = False
    # 运行态
    state: str = "idle"                  # idle | exposing | downloading | error
    is_exposing: bool = False
    exposure_elapsed_s: float = 0.0
    exposure_total_s: float = 0.0
    progress: float = 0.0                # 0..1
    capture_mode: str = "single"         # single | loop | sequence
    last_image_id: Optional[int] = None
    controls: list[CameraControl] = Field(default_factory=list)


class ImageMeta(BaseModel):
    image_id: int
    width: int
    height: int
    exposure_s: float
    gain: int
    offset: int
    bin: int
    filter: str = ""
    temperature: float = 0.0
    mean: float = 0.0
    hfr: float = 0.0                     # 半通量半径(对焦质量)
    stars: int = 0
    captured_at: str = ""
    target: str = ""
    image_url: str = ""
    histogram: list[int] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# 赤道仪
# --------------------------------------------------------------------------- #
class MountState(BaseModel):
    connected: bool = False
    name: str = ""
    # 位置
    ra_hours: float = 0.0
    dec_degrees: float = 0.0
    ra_text: str = ""
    dec_text: str = ""
    altitude: float = 0.0
    azimuth: float = 0.0
    lst_hours: float = 0.0
    lst_text: str = ""
    side_of_pier: str = "unknown"        # east | west | unknown
    # 运行态
    tracking: bool = False
    tracking_mode: str = "sidereal"      # sidereal | lunar | solar | king | stopped
    slewing: bool = False
    at_park: bool = False
    at_home: bool = False
    is_moving: bool = False
    # 站点
    site_lat: float = 0.0
    site_lng: float = 0.0
    site_elev: float = 0.0
    # 目标 / 子午
    target_name: str = ""
    target_ra_hours: Optional[float] = None
    target_dec_degrees: Optional[float] = None
    time_to_meridian_h: Optional[float] = None
    capabilities: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# 调焦 + 自动对焦
# --------------------------------------------------------------------------- #
class FocuserState(BaseModel):
    connected: bool = False
    name: str = ""
    position: int = 0
    max_step: int = 100000
    step_size_um: float = 1.0
    is_moving: bool = False
    temperature: float = 20.0
    temp_comp_available: bool = True
    temp_comp: bool = False
    af_running: bool = False


class AutoFocusPoint(BaseModel):
    position: int
    hfr: float


class AutoFocusResult(BaseModel):
    running: bool = False
    points: list[AutoFocusPoint] = Field(default_factory=list)
    fitting: list[float] = Field(default_factory=list)   # 抛物线系数 a,b,c (hfr=a x^2+b x+c)
    best_position: Optional[int] = None
    best_hfr: Optional[float] = None
    r_squared: Optional[float] = None
    finished_at: str = ""
    error: str = ""


# --------------------------------------------------------------------------- #
# 滤镜轮
# --------------------------------------------------------------------------- #
class FilterSlot(BaseModel):
    position: int
    name: str
    focus_offset: int = 0


class FilterWheelState(BaseModel):
    connected: bool = False
    name: str = ""
    position: int = 0
    is_moving: bool = False
    filters: list[FilterSlot] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# 导星
# --------------------------------------------------------------------------- #
class GuideStep(BaseModel):
    t: float                              # 相对秒
    ra_raw: float                         # 像素
    dec_raw: float
    ra_dist: float                        # 角秒
    dec_dist: float
    ra_duration: float = 0.0              # ms 修正脉冲
    dec_duration: float = 0.0


class GuiderState(BaseModel):
    connected: bool = False
    name: str = ""
    state: str = "idle"                  # idle | calibrating | guiding | dithering | lost
    pixel_scale: float = 1.0             # 角秒/像素
    rms_ra: float = 0.0                  # 角秒
    rms_dec: float = 0.0
    rms_total: float = 0.0
    can_dither: bool = True
    settling: bool = False


# --------------------------------------------------------------------------- #
# 旋转器 / 圆顶 / 平场 / 开关 / 天气 / 安全
# --------------------------------------------------------------------------- #
class RotatorState(BaseModel):
    connected: bool = False
    name: str = ""
    position: float = 0.0                # 天空角(度)
    mechanical_position: float = 0.0
    is_moving: bool = False
    reverse: bool = False


class DomeState(BaseModel):
    connected: bool = False
    name: str = ""
    azimuth: float = 0.0
    shutter: str = "closed"              # open|closed|opening|closing|error
    slewing: bool = False
    at_park: bool = False
    following: bool = False


class FlatDeviceState(BaseModel):
    connected: bool = False
    name: str = ""
    cover: str = "closed"                # open|closed|unknown|notpresent
    light_on: bool = False
    brightness: int = 0
    max_brightness: int = 100


class SwitchItem(BaseModel):
    id: int
    name: str
    value: float
    min: float = 0.0
    max: float = 1.0
    writable: bool = False


class SwitchState(BaseModel):
    connected: bool = False
    name: str = ""
    switches: list[SwitchItem] = Field(default_factory=list)


class WeatherState(BaseModel):
    connected: bool = False
    name: str = ""
    temperature: float = 0.0
    humidity: float = 0.0
    pressure: float = 0.0
    wind_speed: float = 0.0
    wind_direction: float = 0.0
    cloud_cover: float = 0.0
    dew_point: float = 0.0
    sky_quality: float = 0.0


class SafetyState(BaseModel):
    connected: bool = False
    name: str = ""
    is_safe: bool = True


# --------------------------------------------------------------------------- #
# 序列 / 计划
# --------------------------------------------------------------------------- #
class SequenceExposure(BaseModel):
    """目标下的一组同质曝光(每滤镜一行)。"""
    filter: str = "L"
    exposure_s: float = 60.0
    gain: int = 100
    offset: int = 50
    bin: int = 1
    count: int = 10
    completed: int = 0
    type: str = "LIGHT"                   # LIGHT|DARK|FLAT|BIAS


class SequenceTarget(BaseModel):
    name: str = ""
    ra_hours: float = 0.0
    dec_degrees: float = 0.0
    rotation: float = 0.0
    exposures: list[SequenceExposure] = Field(default_factory=list)
    # 触发/条件(简化版)
    dither_every: int = 1                 # 每 N 张抖动一次,0=关
    autofocus_on_temp_delta: float = 1.0  # 温变(°C)触发对焦,0=关
    autofocus_on_filter_change: bool = True
    meridian_flip: bool = True


class SequencePlan(BaseModel):
    name: str = "未命名计划"
    cool_camera_to: Optional[float] = None
    warm_at_end: bool = True
    park_at_end: bool = True
    targets: list[SequenceTarget] = Field(default_factory=list)
    # 高级触发 / 条件(对标 NINA 的 loop / 安全 / 质量 / 暮光)
    loop_count: int = 1                   # 整份计划重复轮数
    abort_on_unsafe: bool = False         # 安全监视器变不安全即中止
    reject_hfr_over: float = 0.0          # HFR 超此值的帧标记为废帧, 0=关
    start_at_sun_altitude: Optional[float] = None  # 日高度低于此值才开始(如 -12)
    stop_at_sun_altitude: Optional[float] = None   # 日高度高于此值即停止(如 -6)


class SequenceState(BaseModel):
    plan: SequencePlan = Field(default_factory=SequencePlan)
    status: str = "idle"                 # idle|running|paused|finished|error|waiting
    running: bool = False
    current_target_index: int = -1
    current_exposure_index: int = -1
    current_loop: int = 0                 # 当前第几轮(从 1 起)
    current_action: str = ""
    total_frames: int = 0
    completed_frames: int = 0
    rejected_frames: int = 0             # 被质量条件废弃的帧
    progress: float = 0.0
    started_at: str = ""
    estimated_remaining_s: float = 0.0
    log: list[str] = Field(default_factory=list)


class PlateSolveResult(BaseModel):
    ok: bool = False
    solved: bool = False
    ra_hours: float = 0.0
    dec_degrees: float = 0.0
    ra_text: str = ""
    dec_text: str = ""
    rotation: float = 0.0                 # 视场旋转角(度)
    error: str = ""


# --------------------------------------------------------------------------- #
# 构图
# --------------------------------------------------------------------------- #
class FramingTarget(BaseModel):
    name: str
    type: str = ""                       # 星系/星云/星团...
    ra_hours: float
    dec_degrees: float
    magnitude: Optional[float] = None
    size_arcmin: Optional[float] = None
    catalog: str = ""


class FramingState(BaseModel):
    target_name: str = ""
    ra_hours: float = 0.0
    dec_degrees: float = 0.0
    rotation: float = 0.0
    fov_width_deg: float = 1.0
    fov_height_deg: float = 0.7


# --------------------------------------------------------------------------- #
# 事件
# --------------------------------------------------------------------------- #
class Event(BaseModel):
    event: str                            # 形如 CAMERA-CONNECTED / IMAGE-SAVE / STATE-UPDATED
    time: str
    domain: str = ""                      # camera/mount/... 便于前端按域刷新
    data: dict[str, Any] = Field(default_factory=dict)
