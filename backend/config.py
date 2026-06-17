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

    # PHD2 事件服务器(与 NINA 连 PHD2 同一端口,多客户端并存)。
    # NINA 的 ninaAPI 不暴露导星画面,只能直连 PHD2 的 get_star_image RPC 取
    # 它跟踪的星点附近那一块裁切(整幅传感器画面 PHD2 不开放)。
    phd2_host: str = os.environ.get("NINAWEB_PHD2_HOST", "127.0.0.1")
    phd2_port: int = int(os.environ.get("NINAWEB_PHD2_PORT", "4400"))
    # 请求裁切边长(像素,≥15;PHD2 以星点为中心裁,实际不超过此值且受帧边界限制)
    phd2_star_size: int = int(os.environ.get("NINAWEB_PHD2_STAR_SIZE", "200"))

    # 观测站点(模拟引擎与坐标换算用)—— 默认乌兰察布远程台附近
    site_lat: float = float(os.environ.get("NINAWEB_LAT", "41.0"))
    site_lng: float = float(os.environ.get("NINAWEB_LNG", "113.1"))
    site_elev: float = float(os.environ.get("NINAWEB_ELEV", "1400"))

    # ── 赤道仪安全护栏(防打腿)──────────────────────────────────────
    # slew/goto 目标地平高度低于此值(度)直接拒绝。0 = 仅拒地平线以下。
    mount_min_alt_deg: float = float(os.environ.get("NINAWEB_MOUNT_MIN_ALT_DEG", "0"))
    # 过中天限位(度):>0 时,目标在「当前墩侧」越过中天进入配重上扬区超过该角度→拒绝。
    # <=0 关闭。依赖 ASCOM SideOfPier 约定(pierEast=镜在东·常态看西 HA>0;pierWest 反之),
    # 不同驱动可能相反 —— 务必保留 NINA 自身中天翻转作权威保护,并用一次已知指向核对方向。
    mount_meridian_limit_deg: float = float(os.environ.get("NINAWEB_MOUNT_MERIDIAN_LIMIT_DEG", "0"))
    # 远程启动序列:默认禁用(序列内 GOTO 在 NINA 内执行,不经本站 _slew_safety 护栏,
    # 只能靠 NINA 自身中天翻转/限位)。确需远程一键启动且已在 NINA 配好安全机制时设为 1。
    allow_sequence_start: bool = _bool("NINAWEB_ALLOW_SEQUENCE_START", False)

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
