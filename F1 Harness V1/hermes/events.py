"""进程内的事件广播：聊天逐字输出、状态变化推给网页（SSE），关卡待审等推系统通知。"""
import asyncio
import json
import logging
import subprocess
from collections import defaultdict

log = logging.getLogger("hermes.events")


class Broker:
    def __init__(self) -> None:
        self._subs: dict[str, set[asyncio.Queue]] = defaultdict(set)

    def subscribe(self, channel: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subs[channel].add(q)
        return q

    def unsubscribe(self, channel: str, q: asyncio.Queue) -> None:
        self._subs[channel].discard(q)

    def publish(self, channel: str, event: str, data: dict) -> None:
        payload = {"event": event, "data": data}
        for q in list(self._subs.get(channel, ())):
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                pass

    def listeners(self, channel: str) -> int:
        return len(self._subs.get(channel, ()))


broker = Broker()


def macos_notify(title: str, body: str) -> None:
    """网页没开着时的兜底系统通知；参数经 json 转义，避免注入 AppleScript。"""
    script = f"display notification {json.dumps(body, ensure_ascii=False)} with title {json.dumps(title, ensure_ascii=False)}"
    try:
        subprocess.run(["/usr/bin/osascript", "-e", script], timeout=10, check=False, capture_output=True)
    except Exception as e:
        log.warning("系统通知失败：%s", e)


def notify(title: str, body: str, episode_id: int | None = None) -> None:
    """网页开着就发网页通知（点击能跳到会话）；否则发系统通知。"""
    if broker.listeners("*"):
        broker.publish("*", "notify", {"title": title, "body": body, "episode_id": episode_id})
    else:
        macos_notify(title, body)
