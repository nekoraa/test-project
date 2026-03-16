import asyncio
import json
import time
import uuid
import random
from aiohttp import web
import aiohttp_cors

# --- 全局配置 ---
PORT = 3000
HEARTBEAT_INTERVAL = 30  # 心跳间隔（秒）
MESSAGE_TTL_MS = 1800 * 1000  # 消息有效期 1800 秒（转为毫秒）

KEYS = {
    'MANUAL_API_KEY': 'manual',
    'AI_API_KEY': 'ai'
}

# 内存映射表
connected_anchors = {}
admin_connections = {}
message_history = {}


# --- 工具函数 ---

def get_auth_source(request):
    """从 Header 校验 API Key 并返回对应的 source"""
    api_key = request.headers.get('X-API-Key') or request.headers.get('x-api-key')
    return KEYS.get(api_key)

def save_message(anchor_id, msg_obj):
    """将消息存入历史缓存（模拟 Redis 半持久化）"""
    if anchor_id not in message_history:
        message_history[anchor_id] = []
    message_history[anchor_id].append(msg_obj)
    # 每次存入时清理一下过期数据
    clean_expired_messages(anchor_id)

def clean_expired_messages(anchor_id):
    """清理超过 1800 秒的历史消息"""
    if anchor_id in message_history:
        current_time_ms = int(time.time() * 1000)
        message_history[anchor_id] = [
            msg for msg in message_history[anchor_id]
            if (current_time_ms - msg['timestamp']) <= MESSAGE_TTL_MS
        ]

def get_valid_history(anchor_id):
    """获取有效的全量历史消息"""
    clean_expired_messages(anchor_id)
    return message_history.get(anchor_id, [])


# --- 路由处理器 ---

async def ws_handler(request):
    anchor_id = request.query.get('anchorId')
    ws_type = request.query.get('type')  # admin 或 live

    if not anchor_id or not ws_type:
        return web.Response(status=400, text="Missing required parameters: type or anchorId")

    ws = web.WebSocketResponse()
    await ws.prepare(request)

    # --------------------
    # 主播端连接
    # --------------------
    if ws_type == "live":
        if anchor_id in connected_anchors:
            old_ws = connected_anchors[anchor_id]
            print(f"[WS] 主播 {anchor_id} 重复连接，断开旧连接")
            await old_ws.close(code=4001, message=b'REPLACED_BY_NEW_CONNECTION')

        connected_anchors[anchor_id] = ws
        print(f"[WS] 主播 {anchor_id} 已连接")

    # --------------------
    # 管理端连接
    # --------------------
    elif ws_type == "admin":
        if anchor_id not in admin_connections:
            admin_connections[anchor_id] = []

        admin_connections[anchor_id].append(ws)
        print(f"[WS] 管理端连接 {anchor_id}")

    # --------------------
    # 下发历史消息
    # --------------------
    history = get_valid_history(anchor_id)

    await ws.send_json({
        "type": "all_messages",
        "data": history
    })

    print(f"[WS] 已为 {anchor_id} 下发历史消息 {len(history)} 条")

    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                    if data.get('message') == 'pong' and data.get('success') is True:
                        print(f"[Heartbeat] 收到 {anchor_id} pong")
                except:
                    pass
    finally:

        # 主播断开
        if ws_type == "live":
            if connected_anchors.get(anchor_id) == ws:
                del connected_anchors[anchor_id]
                print(f"[WS] 主播 {anchor_id} 断开")

        # 管理端断开
        elif ws_type == "admin":
            if anchor_id in admin_connections:
                admin_connections[anchor_id].remove(ws)
                if not admin_connections[anchor_id]:
                    del admin_connections[anchor_id]

                print(f"[WS] 管理端 {anchor_id} 断开")

    return ws


async def push_msg(request):
    """2. 推送提词消息 (内容源 -> 提词器)"""
    source = get_auth_source(request)
    if not source:
        return web.json_response({
            'error': 'UNAUTHORIZED',
            'message': 'API Key is missing or invalid'
        }, status=401)

    try:
        data = await request.json()
        anchor_id = data.get('anchorId')
        content = data.get('content')
    except Exception:
        return web.json_response({
            'error': 'INVALID_PARAM',
            'message': 'Request body must be valid JSON'
        }, status=400)

    if not anchor_id or not content:
        return web.json_response({
            'error': 'INVALID_PARAM',
            'message': 'Missing anchorId or content'
        }, status=400)

    # 目标主播不在线处理
    if anchor_id not in connected_anchors:
        return web.json_response({
            'error': 'ANCHOR_OFFLINE',
            'message': 'Anchor is not connected'
        }, status=404)

    # 构造单条增量消息
    msg_obj = {
        "type": "recent_messages",
        "msgId": str(uuid.uuid4()),
        "source": source,
        "content": content,
        "timestamp": int(time.time() * 1000)
    }

    # 持久化（1800秒）
    save_message(anchor_id, msg_obj)

    # 推送给主播 WebSocket
    ws = connected_anchors[anchor_id]
    try:
        await ws.send_json(msg_obj)
        return web.json_response({'success': True, 'msgId': msg_obj['msgId']})
    except Exception as e:
        print(f"[Push] 发送给 {anchor_id} 失败: {e}")
        return web.json_response({
            'error': 'INTERNAL_ERROR',
            'message': 'Failed to send message via WebSocket'
        }, status=500)


async def get_live_list(request):
    """3. 拉取在线直播间列表"""
    # 限制只有管理后台能拉取
    api_key = request.headers.get('X-API-Key') or request.headers.get('x-api-key')
    if api_key != 'MANUAL_API_KEY':
        return web.json_response({
            'error': 'UNAUTHORIZED',
            'message': 'API Key is missing or invalid'
        }, status=401)

    return web.json_response({
        'success': True,
        'liveList': list(connected_anchors.keys())
    })


# --- 后台任务 ---

async def heartbeat_task(app):
    """后台任务：定时向所有客户端发送 ping 心跳"""
    print("[Task] 心跳发送任务已启动")
    try:
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            if not connected_anchors:
                continue

            ping_msg = {
                "type": "health",
                "content": "ping"
            }

            for anchor_id, ws in list(connected_anchors.items()):
                try:
                    await ws.send_json(ping_msg)
                except Exception as e:
                    print(f"[Heartbeat] 向 {anchor_id} 发送失败: {e}")
    except asyncio.CancelledError:
        pass


# 模拟的 AI 建议库
AI_TIPS = [
    "【AI分析】直播间目前热度攀升，有几位观众在问什么时候上福利链接。",
    "【AI监控】观众平均停留时长增加，建议此时引导大家点亮粉丝灯牌。",
    "【AI弹幕分析】观众对当前讲解的商品非常感兴趣，建议详细说明一下材质和售后。",
    "【AI提示】检测到弹幕多次提及“价格”，建议重申一次专属优惠。"
]

async def simulate_ai_behavior(app):
    """模拟 AI 自动推送逻辑 (供测试观察使用)"""
    try:
        while True:
            await asyncio.sleep(45)  # AI 消息频率
            for anchor_id, ws in list(connected_anchors.items()):
                msg_obj = {
                    "type": "recent_messages",
                    "msgId": str(uuid.uuid4()),
                    "source": "ai",
                    "content": random.choice(AI_TIPS),
                    "timestamp": int(time.time() * 1000)
                }
                save_message(anchor_id, msg_obj)
                try:
                    await ws.send_json(msg_obj)
                    print(f"[AI 自动推送] -> {anchor_id}")
                except:
                    pass
    except asyncio.CancelledError:
        pass


async def start_background_tasks(app):
    app['heartbeat_task'] = asyncio.create_task(heartbeat_task(app))
    app['ai_task'] = asyncio.create_task(simulate_ai_behavior(app))

async def cleanup_background_tasks(app):
    app['heartbeat_task'].cancel()
    app['ai_task'].cancel()
    await asyncio.gather(app['heartbeat_task'], app['ai_task'], return_exceptions=True)


# --- App 初始化 ---

app = web.Application()

# 注册路由
app.router.add_get('/ws', ws_handler)
app.router.add_post('/push', push_msg)
app.router.add_post('/getLiveList', get_live_list)

# 注册后台任务
app.on_startup.append(start_background_tasks)
app.on_cleanup.append(cleanup_background_tasks)

# CORS 配置
cors = aiohttp_cors.setup(app, defaults={
    "*": aiohttp_cors.ResourceOptions(
        allow_credentials=True,
        expose_headers="*",
        allow_headers="*"
    )
})
for route in list(app.router.routes()):
    cors.add(route)

if __name__ == '__main__':
    print(f"Teleprompter Mock Server (v1.0) running on http://localhost:{PORT}")
    web.run_app(app, port=PORT)