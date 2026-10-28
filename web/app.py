"""Web chat interface for the Agentic AI Registrar Office.

    .venv/bin/uvicorn web.app:app --port 8000      then open http://localhost:8000

Each browser login gets its own Registrar session (one ClaudeSDKClient bound to
that user's role). Agent activity is streamed to the page over a WebSocket.
"""

import secrets
from dataclasses import dataclass
from pathlib import Path

from claude_agent_sdk import AssistantMessage, ClaudeSDKClient, ResultMessage, TextBlock, ToolUseBlock
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from registrar.agents import build_options
from registrar.db import CURRENT_SEMESTER, REGISTRATION_SEMESTER, connect
from registrar.service import ROLES, PermissionDenied, Registrar, User

STATIC = Path(__file__).parent / "static"
app = FastAPI(title="Agentic AI Registrar Office")
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@dataclass
class Session:
    user: User
    client: ClaudeSDKClient


SESSIONS: dict[str, Session] = {}


class LoginRequest(BaseModel):
    role: str
    id: str | None = None


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/info")
async def info():
    conn = connect()
    students = [dict(r) for r in conn.execute("SELECT id, name FROM students ORDER BY id LIMIT 8")]
    profs = [dict(r) for r in conn.execute("SELECT id, name, department FROM professors ORDER BY id")]
    advisors = [r[0] for r in conn.execute("SELECT DISTINCT advisor_id FROM students ORDER BY 1")]
    return {"roles": ROLES, "current": CURRENT_SEMESTER, "registration": REGISTRATION_SEMESTER,
            "sample_students": students, "professors": profs, "advisors": advisors}


@app.post("/api/login")
async def login(req: LoginRequest):
    conn = connect()
    try:
        user = Registrar.login(conn, req.role, (req.id or "").strip().upper() or None)
    except PermissionDenied as e:
        raise HTTPException(401, str(e))
    client = ClaudeSDKClient(options=build_options(conn, user))
    await client.connect()
    token = secrets.token_urlsafe(24)
    SESSIONS[token] = Session(user, client)
    return {"token": token, "name": user.name, "role": user.role, "id": user.id}


@app.post("/api/logout/{token}")
async def logout(token: str):
    session = SESSIONS.pop(token, None)
    if session:
        await session.client.disconnect()
    return {"ok": True}


@app.websocket("/ws/{token}")
async def chat(ws: WebSocket, token: str):
    session = SESSIONS.get(token)
    await ws.accept()
    if session is None:
        await ws.send_json({"type": "error", "text": "Session expired. Please log in again."})
        await ws.close()
        return
    agent_names: dict[str, str] = {}  # Agent tool_use id -> specialist name
    try:
        while True:
            question = (await ws.receive_json()).get("message", "").strip()
            if not question:
                continue
            await session.client.query(question)
            async for msg in session.client.receive_response():
                if isinstance(msg, AssistantMessage):
                    has_tool = any(isinstance(b, ToolUseBlock) for b in msg.content)
                    for block in msg.content:
                        if isinstance(block, ToolUseBlock) and block.name == "Agent":
                            agent_names[block.id] = block.input.get("subagent_type", "agent")
                            await ws.send_json({"type": "delegate", "agent": agent_names[block.id],
                                                "description": block.input.get("description", "")})
                        elif isinstance(block, ToolUseBlock):
                            await ws.send_json({"type": "tool", "name": block.name.split("__")[-1],
                                                "agent": agent_names.get(msg.parent_tool_use_id, "registrar"),
                                                "input": block.input})
                        elif isinstance(block, TextBlock) and msg.parent_tool_use_id is None:
                            # text alongside a tool call is a progress note; otherwise it is the answer
                            await ws.send_json({"type": "note" if has_tool else "answer", "text": block.text})
                elif isinstance(msg, ResultMessage):
                    await ws.send_json({"type": "done", "cost": msg.total_cost_usd,
                                        "seconds": round(msg.duration_ms / 1000, 1)})
    except WebSocketDisconnect:
        pass


@app.on_event("shutdown")
async def shutdown():
    for s in SESSIONS.values():
        await s.client.disconnect()
