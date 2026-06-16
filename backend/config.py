"""运行配置 —— 全部可用环境变量覆盖,便于在工控机上部署。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
FRONTEND_DIR = BASE_DIR.parent / "frontend"
STATE_DIR = Path(os.environ.get("NINAWEB_STATE", BASE_DIR.parent / "state"))


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    # 服务
    host: str = os.environ.get("NINAWEB_HOST", "0.0.0.0")          # 0.0.0.0 = 局域网可达
    port: int = int(os.environ.get("NINAWEB_PORT", "8788"))

    # 后端 Provider: "sim" 模拟引擎 | "live" 对接真实 NINA Advanced API
    provider: str = os.environ.get("NINAWEB_PROVIDER", "sim")

    # 只读监控模式:为真时服务端硬性禁用一切写操作(连接/动作/拍摄/板解算/序列),
    # 仅允许读取状态与影像。接真机但暂不允许远程改参数/控制时务必开启。
    readonly: bool = _bool("NINAWEB_READONLY", False)

    # 按域解禁:即便 readonly=True,这些设备域仍允许写操作(逗号分隔)。
    # 例 NINAWEB_WRITABLE_DOMAINS=camera → 只读监控下唯独相机可控,其余仍只读。
    # 取值用设备域名(camera/mount/focuser/...)。空集=完全只读。
    writable_domains: frozenset = frozenset(
        x.strip() for x in os.environ.get("NINAWEB_WRITABLE_DOMAINS", "").split(",") if x.strip())

    # 真实 NINA Advanced API(provider=live 时使用)
    nina_base_url: str = os.environ.get("NINAWEB_NINA_URL", "http://127.0.0.1:1888")
    nina_api_path: str = "/v2/api"
    nina_socket_path: str = "/v2/socket"

    # 观测站点(模拟引擎与坐标换算用)—— 默认乌兰察布远程台附近
    site_lat: float = float(os.environ.get("NINAWEB_LAT", "41.0"))
    site_lng: float = float(os.environ.get("NINAWEB_LNG", "113.1"))
    site_elev: float = float(os.environ.get("NINAWEB_ELEV", "1400"))

    # 协作锁租约(秒)
    control_lease_seconds: int = 45

    # 模拟引擎 tick 频率
    sim_tick_hz: float = 10.0

    # 事件环形缓冲容量
    event_ring_size: int = 500

    state_dir: Path = field(default_factory=lambda: STATE_DIR)
    frontend_dir: Path = field(default_factory=lambda: FRONTEND_DIR)

    def __post_init__(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        # 防呆:解禁域拼错会"静默不生效"(fail-closed,无安全风险但难排障),启动期告警
        known = {"camera", "mount", "focuser", "filterwheel", "guider", "rotator",
                 "framing", "dome", "flatdevice", "switch", "sequence"}
        unknown = set(self.writable_domains) - known
        if unknown:
            import sys
            print(f"[配置告警] NINAWEB_WRITABLE_DOMAINS 含未知域 {sorted(unknown)} —— "
                  f"将不会生效;已知域:{sorted(known)}", file=sys.stderr)


settings = Settings()
