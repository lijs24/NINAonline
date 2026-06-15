"""进程内事件总线 + 历史环形缓冲。

- Gateway / 模拟引擎 通过 publish() 推送 NINA 式事件;
- WebSocket 端点 通过 subscribe() 拿到一个异步队列,实时转发给浏览器;
- /api/events 通过 history() 回放最近事件(对应 NINA 的 event-history)。
"""
from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime
from typing import Any

from gateway.models import Event


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class EventBus:
    def __init__(self, ring_size: int = 500) -> None:
        self._subscribers: set[asyncio.Queue[Event]] = set()
        self._history: deque[Event] = deque(maxlen=ring_size)
        self._seq = 0

    # -- 发布 ------------------------------------------------------------- #
    def publish(self, event: str, domain: str = "", **data: Any) -> Event:
        self._seq += 1
        evt = Event(event=event, time=_now_iso(), domain=domain,
                    data={"seq": self._seq, **data})
        self._history.append(evt)
        for q in list(self._subscribers):
            try:
                q.put_nowait(evt)
            except asyncio.QueueFull:
                pass
        return evt

    def state_changed(self, domain: str, **data: Any) -> Event:
        """约定:STATE-UPDATED 让前端按域做轻量重拉。"""
        return self.publish("STATE-UPDATED", domain=domain, **data)

    # -- 订阅 ------------------------------------------------------------- #
    def subscribe(self) -> asyncio.Queue[Event]:
        q: asyncio.Queue[Event] = asyncio.Queue(maxsize=200)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[Event]) -> None:
        self._subscribers.discard(q)

    # -- 历史 ------------------------------------------------------------- #
    def history(self, since_seq: int = 0) -> list[Event]:
        return [e for e in self._history if e.data.get("seq", 0) > since_seq]


# 单例(app 启动时创建并注入)
bus = EventBus()
