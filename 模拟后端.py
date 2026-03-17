import asyncio
import json
import time
import uuid
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
    "agent": {},  # AI Agent (1:N)
    "liveMic": {}  # 主播麦克风音频流 (1:1) - 新增
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

async def broadcast_live_list():
    """广播当前在线的主播列表给所有的管理端 (解决主播上下线不同步问题)"""
    live_anchors = list(connections["live"].keys())
    event_payload = format_event("getLiveList", {"anchorIds": live_anchors})

    for admin_id, ws_list in connections["admin"].items():
        for ws in ws_list:
            if not ws.closed:
                try:
                    await ws.send_json(event_payload)
                except Exception as e:
                    print(f"[广播错误] 发送在线列表给管理员 {admin_id} 失败: {e}")


async def broadcast_to_anchor(anchor_id, event_name, data):
    """推送给特定主播端"""
    ws = connections["live"].get(anchor_id)
    if ws and not ws.closed:
        await ws.send_json(format_event(event_name, data))
        return True
    return False


async def handle_upstream_msg(ws, client_type, anchor_id, token, payload):
    """处理客户端发上来的事件 (文档7.4)"""
    event = payload.get("event")
    req_id = payload.get("requestId")
    data = payload.get("data", {})

    if event == "pong":
        pass  # 忽略常规 pong 打印以免刷屏
        # print(f"[心跳] 收到 pong 回应，来自 {client_type}:{anchor_id}")

    elif event == "push":
        # 管理端推送消息到直播端 (7.4.3)
        target_anchor = data.get("anchorId")
        content = data.get("content")

        msg_source = "ai" if token == "AI_API_KEY" else "manual"

        msg_item = save_and_clean_history(target_anchor, {
            "source": msg_source,
            "content": content
        })

        # 发送增量给主播 (7.3.3)
        success = await broadcast_to_anchor(target_anchor, "recentMessages", {"messages": [msg_item]})

        # 回执给发送方
        if success:
            resp = format_response(ok=True, code="OK", message="success", data={"msgId": msg_item["msgId"]},
                                   request_id=req_id)
        else:
            resp = format_response(ok=False, code="ANCHOR_OFFLINE", message="Anchor is not connected", data=None,
                                   request_id=req_id)

        await ws.send_json(resp)
        print(
            f"[消息推送] {client_type}:{anchor_id}({msg_source}) -> 主播:{target_anchor} | 状态: {'成功' if success else '失败(不在线)'}")

    elif event == "sendSound":
        print(f"[语音转写] 主播 {anchor_id}: {data.get('text')} (置信度: {data.get('sttConfidence')})")
        await ws.send_json(format_response(request_id=req_id))

    elif event == "sendChat":
        print(f"[对话请求] 主播 {anchor_id} 询问 Agent: {data.get('text')}")
        await asyncio.sleep(1)
        ai_msg = save_and_clean_history(anchor_id, {"source": "ai",
                                                    "content": f"关于 '{data.get('text')}'，建议您可以从价格优势切入。"})
        await broadcast_to_anchor(anchor_id, "recentMessages", {"messages": [ai_msg]})
        await ws.send_json(format_response(request_id=req_id))


# --- 路由处理器 ---

async def ws_handler(request):
    client_type = request.query.get('clientType')  # live / admin / agent / liveMic
    anchor_id = request.query.get('anchorId')
    token = request.query.get('token')

    if not client_type or not anchor_id:
        return web.Response(status=400, text="缺少必要参数: clientType 或 anchorId")

    if client_type not in ["live", "admin", "agent", "liveMic"]:
        return web.Response(status=400, text="非法的 clientType")

    if not token:
        token = "UNKNOWN_TOKEN"

    if client_type in ["admin", "agent"]:
        if token not in ["MANUAL_API_KEY", "AI_API_KEY"]:
            print(f"[鉴权拦截] 拒绝 {client_type}:{anchor_id} 连接，无效的 Token: {token}")
            return web.Response(status=401, text="Unauthorized: Invalid Token")

    ws = web.WebSocketResponse()
    await ws.prepare(request)

    # 1. 注册连接
    if client_type in ["live", "liveMic"]:
        # 主播端和麦克风端互斥逻辑 (1:1)
        if anchor_id in connections[client_type]:
            old_ws = connections[client_type][anchor_id]
            print(f"[连接] 主播/音频端 {anchor_id} 重复登录，正在断开旧连接 ({client_type})")
            await old_ws.close(code=4001, message=b'REPLACED_BY_NEW_CONNECTION')

        connections[client_type][anchor_id] = ws
        print(f"[连接] {client_type} 端已接入: {anchor_id}")

        if client_type == "live":
            await broadcast_live_list()

    else:
        # 管理端 / Agent 允许多开 (1:N)
        if anchor_id not in connections[client_type]:
            connections[client_type][anchor_id] = []
        connections[client_type][anchor_id].append(ws)
        print(f"[连接] {client_type} 端已接入: {anchor_id} (Token验证通过)")

    # 2. 除了音频流节点，其余节点建立后下发消息历史
    if client_type != "liveMic":
        history = message_history.get(anchor_id, [])
        await ws.send_json(format_event("allMessages", {"messages": history}))

        if client_type == "admin":
            await ws.send_json(format_event("getLiveList", {"anchorIds": list(connections["live"].keys())}))

    # 音频流专用的状态变量 (根据 7.5.2 协议解析)
    expecting_binary = False
    current_stream_id = ""
    current_seq = 0
    audio_bytes_received = 0

    # 3. 消息循环处理
    try:
        async for msg in ws:
            # === 处理 JSON 文本帧 ===
            if msg.type == web.WSMsgType.TEXT:
                try:
                    payload = json.loads(msg.data)

                    # 音频专属通道逻辑
                    if client_type == "liveMic":
                        event = payload.get("event")
                        data_payload = payload.get("data", {})

                        if event == "streamStart":
                            current_stream_id = data_payload.get("streamId")
                            codec = data_payload.get("codec")
                            print(f"🎤 [麦克风] {anchor_id} 开始音频推流 (流ID: {current_stream_id}, 编码: {codec})")
                            audio_bytes_received = 0

                        elif event == "streamChunk":
                            # 收到 chunk 描述后，明确下一个帧必须是 Binary 二进制音频数据
                            expecting_binary = True
                            current_seq = data_payload.get("seq", 0)

                        elif event == "streamEnd":
                            print(f"🛑 [麦克风] {anchor_id} 结束音频推流。共接收: {audio_bytes_received / 1024:.2f} KB")

                    else:
                        # 原有的常规消息逻辑
                        await handle_upstream_msg(ws, client_type, anchor_id, token, payload)

                except Exception as e:
                    print(f"[异常] 解析文本消息失败: {e} | 数据: {msg.data}")

            # === 处理 BINARY 二进制帧 (录音数据) ===
            elif msg.type == web.WSMsgType.BINARY:
                if client_type == "liveMic" and expecting_binary:
                    # 收到对应的音频数据
                    audio_chunk = msg.data
                    audio_bytes_received += len(audio_chunk)
                    expecting_binary = False

                    # 为了防止刷屏，每隔 50 个包(约1秒音频)打印一次进度
                    if current_seq % 50 == 0:
                        print(
                            f"   🔊 [音频接收中] {anchor_id} seq={current_seq}, 当前累计: {audio_bytes_received / 1024:.2f} KB")
                else:
                    print(f"[警告] 收到意外的二进制数据！ clientType={client_type}")

    finally:
        # 4. 断开连接清理
        if client_type in ["live", "liveMic"]:
            if connections[client_type].get(anchor_id) == ws:
                del connections[client_type][anchor_id]
                print(f"[断开] {client_type} 端已离开: {anchor_id}")
                if client_type == "live":
                    await broadcast_live_list()
        else:
            if ws in connections[client_type].get(anchor_id, []):
                connections[client_type][anchor_id].remove(ws)
                print(f"[断开] {client_type} 端已离开: {anchor_id}")

    return ws


# --- 后台任务 ---

async def heartbeat_task(app):
    """定时发送 ping (文档7.3.1)"""
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL)
        ping_pkt = format_event("ping", {"nonce": str(uuid.uuid4())[:8]})

        for c_type in connections:
            if c_type in ["live", "liveMic"]:
                for ws in connections[c_type].values():
                    if not ws.closed:
                        await ws.send_json(ping_pkt)
            else:
                for ws_list in connections[c_type].values():
                    for ws in ws_list:
                        if not ws.closed:
                            await ws.send_json(ping_pkt)


async def simulate_ai_broadcast(app):
    """模拟 AI 发现直播间问题并自动推送"""
    while True:
        await asyncio.sleep(60)
        for anchor_id in list(connections["live"].keys()):
            msg_item = save_and_clean_history(anchor_id, {
                "source": "ai",
                "content": f"【AI 建议】检测到弹幕活跃度下降，建议主播引导观众点亮粉丝灯牌。({time.strftime('%H:%M:%S')})"
            })
            await broadcast_to_anchor(anchor_id, "recentMessages", {"messages": [msg_item]})
            # print(f"[AI 自动触发] 向主播 {anchor_id} 推送了话术建议。")


# --- App 启动与关闭 ---

async def on_startup(app):
    print("[系统] 启动后台心跳与 AI 模拟任务...")
    app['heartbeat'] = asyncio.create_task(heartbeat_task(app))
    app['ai_sim'] = asyncio.create_task(simulate_ai_broadcast(app))


async def on_cleanup(app):
    print("[系统] 正在关闭后台任务...")
    app['heartbeat'].cancel()
    app['ai_sim'].cancel()


app = web.Application()
app.router.add_get('/ws', ws_handler)

# CORS 配置
cors = aiohttp_cors.setup(app, defaults={
    "*": aiohttp_cors.ResourceOptions(allow_credentials=True, expose_headers="*", allow_headers="*")
})
for route in list(app.router.routes()):
    cors.add(route)

app.on_startup.append(on_startup)
app.on_cleanup.append(on_cleanup)

if __name__ == '__main__':
    print(f"==================================================")
    print(f"  Content Agent 模拟服务器已启动")
    print(f"  WebSocket 端点: ws://localhost:{PORT}/ws")
    print(f"  支持音频推流频道: clientType=liveMic")
    print(f"==================================================")
    web.run_app(app, port=PORT, access_log=None)