from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter()


@router.websocket("/ws/conversations/{conversation_id}")
async def conversation_socket(websocket: WebSocket, conversation_id: str) -> None:
    await websocket.accept()
    try:
        await websocket.send_json(
            {
                "event_type": "system.connected",
                "conversation_id": conversation_id,
                "ts": datetime.now(timezone.utc).isoformat(),
                "payload": {"message": "WebSocket connected (skeleton)"},
            }
        )
        while True:
            data = await websocket.receive_text()
            await websocket.send_json(
                {
                    "event_type": "system.echo",
                    "conversation_id": conversation_id,
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "payload": {"received": data},
                }
            )
    except WebSocketDisconnect:
        return
