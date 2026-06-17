"""主控/监控协作锁 —— 局域网多用户场景下,保证同一时刻只有一个"主控"能下命令。

沿用旧 asiairbridge 的语义:租约式独占(lease),监控者只读,主控者持锁并需续租。
单台远程台 → 单把锁(rig)。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class Holder:
    session_id: str
    label: str
    client_ip: str
    expires: float

    def view(self) -> dict:
        # 不外泄 session_id —— 否则任何监控端读到后即可冒充主控通过 _guard。
        # 前端判角色用服务端算好的 held_by_self,展示只需 名称/IP。
        return {"display_name": self.label, "client_ip": self.client_ip}


class ControlLock:
    def __init__(self, lease_seconds: int = 45) -> None:
        self.lease = lease_seconds
        self._holder: Optional[Holder] = None

    def _expire_if_needed(self) -> None:
        if self._holder and self._holder.expires < time.time():
            self._holder = None

    def state(self, session_id: str) -> dict:
        self._expire_if_needed()
        held_by_self = bool(self._holder and self._holder.session_id == session_id)
        return {
            "ok": True,
            "lease_seconds": self.lease,
            "server_time": datetime.now().isoformat(timespec="seconds"),
            "controller": self._holder.view() if self._holder else None,
            "held_by_self": held_by_self,
            "role": "controller" if held_by_self else "monitor",
            "available": self._holder is None or held_by_self,
        }

    def claim(self, session_id: str, label: str, client_ip: str, role: str) -> dict:
        self._expire_if_needed()
        if role == "monitor":
            # 释放(若自己持有)
            if self._holder and self._holder.session_id == session_id:
                self._holder = None
            return self.state(session_id)
        # role == controller:抢/续锁
        if self._holder and self._holder.session_id != session_id:
            return self.state(session_id)            # 已被他人占用
        self._holder = Holder(session_id, label or "web", client_ip,
                              time.time() + self.lease)
        return self.state(session_id)

    def is_controller(self, session_id: str) -> bool:
        self._expire_if_needed()
        return bool(self._holder and self._holder.session_id == session_id)

    def can_act(self, session_id: str) -> bool:
        """锁空闲 → 允许;锁被自己持有 → 允许;被他人持有 → 拒绝。"""
        self._expire_if_needed()
        return self._holder is None or self._holder.session_id == session_id
