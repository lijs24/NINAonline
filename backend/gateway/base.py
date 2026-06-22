"""NinaGateway —— 后端与"设备世界"之间的唯一抽象边界。

SimGateway(模拟引擎) 与 LiveGateway(真实 NINA Advanced API) 都实现它。
API 路由只依赖这个接口,因此切换真机/模拟对前端完全透明。

约定:
- get_* 返回 models.py 里的强类型快照(只读);
- *_action(action, params) 执行写操作,返回 {ok, ...} 字典;
- connect/disconnect 接受设备类型字符串(DeviceType);
- 事件由实现方通过 EventBus.publish 主动推送,不在此接口里。
"""
from __future__ import annotations

import abc
from typing import Any

from gateway import models as m


class NinaGateway(abc.ABC):
    mode: str = "base"

    async def start(self) -> None:
        """启动后台任务(模拟 tick / NINA ws 桥接)。"""

    async def stop(self) -> None:
        ...

    # -- 设备生命周期 ----------------------------------------------------- #
    @abc.abstractmethod
    async def list_drivers(self, device_type: str) -> list[m.DriverDescriptor]: ...

    @abc.abstractmethod
    async def connect(self, device_type: str, driver_id: str | None) -> dict[str, Any]: ...

    @abc.abstractmethod
    async def disconnect(self, device_type: str) -> dict[str, Any]: ...

    @abc.abstractmethod
    async def device_summaries(self) -> list[m.DeviceSummary]: ...

    # -- 各域状态 --------------------------------------------------------- #
    @abc.abstractmethod
    async def get_camera(self) -> m.CameraState: ...
    @abc.abstractmethod
    async def get_mount(self) -> m.MountState: ...
    @abc.abstractmethod
    async def get_focuser(self) -> m.FocuserState: ...
    @abc.abstractmethod
    async def get_autofocus(self) -> m.AutoFocusResult: ...
    @abc.abstractmethod
    async def get_filterwheel(self) -> m.FilterWheelState: ...
    @abc.abstractmethod
    async def get_guider(self) -> m.GuiderState: ...
    @abc.abstractmethod
    async def get_guider_graph(self) -> list[m.GuideStep]: ...
    @abc.abstractmethod
    async def get_rotator(self) -> m.RotatorState: ...
    @abc.abstractmethod
    async def get_dome(self) -> m.DomeState: ...
    @abc.abstractmethod
    async def get_flatdevice(self) -> m.FlatDeviceState: ...
    @abc.abstractmethod
    async def get_switch(self) -> m.SwitchState: ...
    @abc.abstractmethod
    async def get_weather(self) -> m.WeatherState: ...
    @abc.abstractmethod
    async def get_safety(self) -> m.SafetyState: ...
    @abc.abstractmethod
    async def get_sequence(self) -> m.SequenceState: ...
    @abc.abstractmethod
    async def get_framing(self) -> m.FramingState: ...

    # -- 各域动作 --------------------------------------------------------- #
    @abc.abstractmethod
    async def camera_action(self, action: str, params: dict) -> dict[str, Any]: ...
    @abc.abstractmethod
    async def mount_action(self, action: str, params: dict) -> dict[str, Any]: ...
    @abc.abstractmethod
    async def focuser_action(self, action: str, params: dict) -> dict[str, Any]: ...
    @abc.abstractmethod
    async def filterwheel_action(self, action: str, params: dict) -> dict[str, Any]: ...
    @abc.abstractmethod
    async def guider_action(self, action: str, params: dict) -> dict[str, Any]: ...
    @abc.abstractmethod
    async def rotator_action(self, action: str, params: dict) -> dict[str, Any]: ...
    @abc.abstractmethod
    async def sequence_action(self, action: str, params: dict) -> dict[str, Any]: ...
    @abc.abstractmethod
    async def framing_action(self, action: str, params: dict) -> dict[str, Any]: ...

    # -- 影像 ------------------------------------------------------------- #
    @abc.abstractmethod
    async def get_image_meta(self, image_id: int | None = None) -> m.ImageMeta | None: ...

    @abc.abstractmethod
    async def get_image_png(self, image_id: int | None = None,
                            stretch: bool = True) -> bytes | None: ...

    async def get_guider_star_image(self) -> dict[str, Any]:
        """导星星点画面(PHD2 get_star_image)。默认 Provider 不支持。
        返回 {available, frame, width, height, star:[x,y], image:dataURL} 或 {available:False}。"""
        return {"available": False, "reason": "该 Provider 未实现导星画面"}

    # -- 辅助设备动作 / 板解算 / 天文台条件(默认未实现, Sim 覆盖) -------- #
    async def dome_action(self, action: str, params: dict) -> dict[str, Any]:
        return {"ok": False, "error": "该 Provider 未实现圆顶动作"}

    async def flatdevice_action(self, action: str, params: dict) -> dict[str, Any]:
        return {"ok": False, "error": "该 Provider 未实现平场动作"}

    async def switch_action(self, action: str, params: dict) -> dict[str, Any]:
        return {"ok": False, "error": "该 Provider 未实现开关动作"}

    async def platesolve(self) -> m.PlateSolveResult:
        return m.PlateSolveResult(ok=False, error="该 Provider 未实现板解算")

    async def get_conditions(self) -> dict[str, Any]:
        """天文台综合条件:日高度、是否安全等。"""
        return {"ok": True, "sun_altitude": None, "is_safe": True}

    # -- 序列设计器(provider 无关纯计算;仅 get_site 各 provider 自实现) -- #
    async def get_site(self) -> m.Site:
        """观测站点坐标。sim 返回配置默认;live 读 NINA /profile/show。"""
        return m.Site()

    async def estimate(self, project: m.Project, card: m.Card | None,
                       clip: m.Clip | None) -> m.EstimateResult:
        """估时:provider 无关,直接委托 designer。clip 优先(取其卡片+覆盖),
        否则用裸 card + project.overhead。"""
        from gateway import designer
        if clip is not None:
            return designer.estimate_clip(project, clip)
        if card is not None:
            return designer.estimate_duration(card, project.overhead)
        return m.EstimateResult()

    async def compile_project(self, project: m.Project) -> m.CompiledSequence:
        from gateway import designer
        return designer.compile_project(project)

    async def twilight(self, date: str, lat: float, lon: float) -> m.TwilightResult:
        from gateway import designer
        return designer.twilight(date, lat, lon)

    # -- 构图检索 / 图像库(默认空实现, Sim 覆盖) ------------------------- #
    async def framing_search(self, q: str) -> list[m.FramingTarget]:
        return []

    async def library_summary(self) -> dict[str, Any]:
        return {"ok": True, "total": 0, "by_filter": {}, "by_target": {}}

    async def library_list(self, **filters: Any) -> list[m.ImageMeta]:
        return []
