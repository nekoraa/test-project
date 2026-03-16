import asyncio
import json
import time
import uuid
import random
from aiohttp import web
import aiohttp_cors

# --- 配置 ---
PORT = 3000
HEARTBEAT_INTERVAL = 30
MESSAGE_TTL_MS = 1800 * 1000

# 内存存储
# connections[clientType][anchorId] = [ws1, ws2...]
connections = {
    "live": {},  # 主播端 (1:1)
    "admin": {},  # 管理端 (1:N)
    "agent": {}  # AI Agent (1:N)
}
message_history = {}


# --- 工具函数 ---

def format_response(ok=True, code="OK", message="success", data=None, request_id=None):
    """符合文档第4节的统一响应结构"""
    return {
        "ok": ok,
        "code": code,
        "message": message,
        "requestId": request_id or f"req-{int(time.time() * 1000)}",
        "data": data
    }


def format_event(event, data, request_id=None):
    """符合文档第7.2节的消息包结构"""
    return {
        "event": event,
        "requestId": request_id,
        "ts": int(time.time() * 1000),
        "data": data
    }


def save_and_clean_history(anchor_id, msg_obj):
    """保存消息并清理过期内容"""
    if anchor_id not in message_history:
        message_history[anchor_id] = []

    # 转换为 Message 模型格式 (文档5.1)
    message_item = {
        "msgId": msg_obj.get("msgId", str(uuid.uuid4())),
        "anchorId": anchor_id,
        "source": msg_obj.get("source", "manual"),
        "content": msg_obj.get("content", ""),
        "createdAt": int(time.time() * 1000)
    }

    message_history[anchor_id].append(message_item)

    # 清理过期
    now = int(time.time() * 1000)
    message_history[anchor_id] = [
        m for m in message_history[anchor_id]
        if (now - m["createdAt"]) <= MESSAGE_TTL_MS
    ]
    return message_item


# --- 业务逻辑 ---

async def broadcast_to_anchor(anchor_id, event_name, data):
    """推送给特定主播端"""
    ws = connections["live"].get(anchor_id)
    if ws and not ws.closed:
        await ws.send_json(format_event(event_name, data))
        return True
    return False


async def handle_upstream_msg(ws, client_type, anchor_id, payload):
    """处理客户端发上来的事件 (文档7.4)"""
    event = payload.get("event")
    req_id = payload.get("requestId")
    data = payload.get("data", {})

    if event == "pong":
        print(f"[Heartbeat] Received pong from {client_type}:{anchor_id}")

    elif event == "push":
        # 管理端推送消息到直播端 (7.4.3)
        target_anchor = data.get("anchorId")
        content = data.get("content")

        msg_item = save_and_clean_history(target_anchor, {
            "source": "manual" if client_type == "admin" else "ai",
            "content": content
        })

        # 发送增量给主播 (7.3.3)
        success = await broadcast_to_anchor(target_anchor, "recentMessages", {"messages": [msg_item]})

        # 回执给发送方
        resp = format_response(ok=success, code="OK" if success else "ANCHOR_OFFLINE",
                               data={"msgId": msg_item["msgId"]}, request_id=req_id)
        await ws.send_json(resp)

    elif event == "sendSound":
        # 语音转写 (7.4.1) -> 通常转发给 Agent 或存入日志
        print(f"[STT] {anchor_id}: {data.get('text')} (conf: {data.get('sttConfidence')})")
        await ws.send_json(format_response(request_id=req_id))

    elif event == "sendChat":
        # 转发到 Agent (7.4.2)
        print(f"[Chat] {anchor_id} asks Agent: {data.get('text')}")
        # 模拟 AI 立即回一个提词
        await asyncio.sleep(1)
        ai_msg = save_and_clean_history(anchor_id, {"source": "ai",
                                                    "content": f"关于'{data.get('text')}'，建议主播可以从价格优势切入。"})
        await broadcast_to_anchor(anchor_id, "recentMessages", {"messages": [ai_msg]})
        await ws.send_json(format_response(request_id=req_id))


# --- 路由处理器 ---

async def ws_handler(request):
    # 获取 Query 参数 (文档3.2)
    client_type = request.query.get('clientType')  # live / admin / agent
    anchor_id = request.query.get('anchorId')
    token = request.query.get('token')

    if not client_type or not anchor_id:
        return web.Response(status=400, text="Missing clientType or anchorId")

    ws = web.WebSocketResponse()
    await ws.prepare(request)

    # 连接约束：主播端互斥
    if client_type == "live":
        if anchor_id in connections["live"]:
            old_ws = connections["live"][anchor_id]
            await old_ws.close(code=4001, message=b'REPLACED_BY_NEW_CONNECTION')
        connections["live"][anchor_id] = ws
    else:
        if anchor_id not in connections[client_type]:
            connections[client_type][anchor_id] = []
        connections[client_type][anchor_id].append(ws)

    print(f"[WS] Connected: {client_type} for anchor {anchor_id}")

    # 1. 连接成功后立即下发历史快照 (7.3.2)
    history = message_history.get(anchor_id, [])
    await ws.send_json(format_event("allMessages", {"messages": history}))

    # 2. 如果是管理端，可能需要在线列表 (7.3.4)
    if client_type == "admin":
        await ws.send_json(format_event("getLiveList", {"anchorIds": list(connections["live"].keys())}))

    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                try:
                    payload = json.loads(msg.data)
                    await handle_upstream_msg(ws, client_type, anchor_id, payload)
                except Exception as e:
                    print(f"Error handling msg: {e}")
    finally:
        # 清理连接
        if client_type == "live":
            if connections["live"].get(anchor_id) == ws:
                del connections["live"][anchor_id]
        else:
            if ws in connections[client_type].get(anchor_id, []):
                connections[client_type][anchor_id].remove(ws)

        print(f"[WS] Disconnected: {client_type} {anchor_id}")

    return ws


# --- 后台任务 ---

async def heartbeat_task(app):
    """定时发送 ping (文档7.3.1)"""
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL)
        ping_pkt = format_event("ping", {"nonce": str(uuid.uuid4())[:8]})

        # 遍历所有 clientType 的所有连接
        for c_type in connections:
            if c_type == "live":
                for ws in connections[c_type].values():
                    await ws.send_json(ping_pkt)
            else:
                for ws_list in connections[c_type].values():
                    for ws in ws_list:
                        await ws.send_json(ping_pkt)


async def simulate_ai_broadcast(app):
    """模拟 AI 发现直播间问题并推送"""
    while True:
        await asyncio.sleep(60)
        for anchor_id in list(connections["live"].keys()):
            msg_item = save_and_clean_history(anchor_id, {
                "source": "ai",
                "content": f"【AI 建议】当前弹幕互动率下降，建议进行抽奖活动。({int(time.time())})"
            })
            await broadcast_to_anchor(anchor_id, "recentMessages", {"messages": [msg_item]})


# --- App 启动 ---

async def on_startup(app):
    app['heartbeat'] = asyncio.create_task(heartbeat_task(app))
    app['ai_sim'] = asyncio.create_task(simulate_ai_broadcast(app))


async def on_cleanup(app):
    app['heartbeat'].cancel()
    app['ai_sim'].cancel()


app = web.Application()
app.router.add_get('/ws', ws_handler)

# CORS
cors = aiohttp_cors.setup(app, defaults={
    "*": aiohttp_cors.ResourceOptions(allow_credentials=True, expose_headers="*", allow_headers="*")
})
for route in list(app.router.routes()):
    cors.add(route)

app.on_startup.append(on_startup)
app.on_cleanup.append(on_cleanup)

if __name__ == '__main__':
    print(f"Content Agent Mock Server running on ws://localhost:{PORT}/ws")
    web.run_app(app, port=PORT)