from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import WebSocket
from starlette.websockets import WebSocketState


class ChannelHub:
    """Un hub par canal : diffusion des messages JSON à tous les abonnés."""

    def __init__(self) -> None:
        self._rooms: dict[int, set[WebSocket]] = {}
        self._lock = asyncio.Lock()

    async def connect(self, channel_id: int, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._rooms.setdefault(channel_id, set()).add(ws)

    async def disconnect(self, channel_id: int, ws: WebSocket) -> None:
        async with self._lock:
            if channel_id in self._rooms:
                self._rooms[channel_id].discard(ws)
                if not self._rooms[channel_id]:
                    del self._rooms[channel_id]

    async def broadcast(self, channel_id: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, default=str)
        async with self._lock:
            clients = list(self._rooms.get(channel_id, ()))
        dead: list[WebSocket] = []
        for ws in clients:
            try:
                if ws.client_state == WebSocketState.CONNECTED:
                    await ws.send_text(data)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._lock:
                room = self._rooms.get(channel_id)
                if room:
                    for ws in dead:
                        room.discard(ws)


hub = ChannelHub()
