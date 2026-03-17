import asyncio
import json
import random
import time
import uuid
import os
import shutil
import wave
import sys
import websockets
from aiohttp import web
import aiohttp_cors

# =====================================================================
# ======================== 语音识别参数配置区 =========================
# =====================================================================
API_KEY = os.getenv("DASHSCOPE_API_KEY")
WS_URL = "wss://dashscope.aliyuncs.com/api-ws/v1/inference/"
MODEL = "paraformer-realtime-v2"
AUDIO_FORMAT = "pcm"
SAMPLE_RATE = 16000
CHANNELS = 1
LANGUAGE_HINTS = ["zh", "en"]
DISFLUENCY_REMOVAL = False
SEMANTIC_PUNCTUATION = False
MAX_SENTENCE_SILENCE = 800
PUNCTUATION_PREDICTION = True
INVERSE_TEXT_NORMALIZATION = True
HEARTBEAT = False
VOCABULARY_ID = ""
COLOR_RED = "\033[91m"
COLOR_RESET = "\033[0m"

# ASR 会话管理：{ streamId: {"ws": websocket, "task_id": str, "recv_task": asyncio.Task} }
asr_sessions = {}

# --- 原有配置 ---
PORT = 3000
HEARTBEAT_INTERVAL = 30
MESSAGE_TTL_MS = 1800 * 1000
RECORDINGS_DIR = "recordings"

connections = {
    "live": {},
    "admin": {},
    "agent": {},
    "liveMic": {}
}
message_history = {}
audio_data_store = {}


# --- 工具函数 ---
def format_response(ok=True, code="OK", message="success", data=None, request_id=None):
    return {
        "ok": ok,
        "code": code,
        "message": message,
        "requestId": request_id or f"req-{int(time.time() * 1000)}",
        "data": data
    }


def format_event(event, data, request_id=None):
    return {
        "event": event,
        "requestId": request_id,
        "ts": int(time.time() * 1000),
        "data": data
    }


def save_and_clean_history(anchor_id, msg_obj):
    if anchor_id not in message_history:
        message_history[anchor_id] = []
    message_item = {
        "msgId": msg_obj.get("msgId", str(uuid.uuid4())),
        "anchorId": anchor_id,
        "source": msg_obj.get("source", "manual"),
        "content": msg_obj.get("content", ""),
        "createdAt": int(time.time() * 1000)
    }
    message_history[anchor_id].append(message_item)
    now = int(time.time() * 1000)
    message_history[anchor_id] = [
        m for m in message_history[anchor_id]
        if (now - m["createdAt"]) <= MESSAGE_TTL_MS
    ]
    return message_item


def save_wav_file(stream_id, pcm_data):
    if not pcm_data:
        return
    file_path = os.path.join(RECORDINGS_DIR, f"{stream_id}.wav")
    try:
        with wave.open(file_path, 'wb') as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(pcm_data)
        print(f"\n💾 [文件保存] 音频已存至: {file_path} (大小: {len(pcm_data) / 1024:.2f} KB)")
    except Exception as e:
        print(f"\n[错误] 保存 WAV 失败: {e}")


# =====================================================================
# ======================== 新增 ASR 处理逻辑 ==========================
# =====================================================================
async def receive_asr_results(ws, anchor_id):
    """接收阿里云百炼的实时识别结果"""
    try:
        async for message in ws:
            res = json.loads(message)
            event = res.get("header", {}).get("event")

            if event == "result-generated":
                sentence = res.get("payload", {}).get("output", {}).get("sentence", {})
                text = sentence.get("text", "")
                is_end = sentence.get("sentence_end", False)

                if not text: continue

                # 清除当前控制台行并覆盖打印
                sys.stdout.write("\r\033[K")

                if is_end:
                    # 完整句子：使用红色打印，并且换行
                    sys.stdout.write(f"{COLOR_RED}[最终] {text}{COLOR_RESET}\n")
                    sys.stdout.flush()
                else:
                    # 其他（中间结果）：默认白色打印，不换行，动态刷新
                    sys.stdout.write(f"[中间] {text}")
                    sys.stdout.flush()

            elif event == "task-finished":
                print(f"\n[ASR] 主播 {anchor_id} 的识别任务已成功结束。")
                break
            elif event == "task-failed":
                error_msg = res.get("header", {}).get("error_message", "未知错误")
                print(f"\n[ASR 错误] 主播 {anchor_id} 识别失败: {error_msg}")
                break
    except websockets.exceptions.ConnectionClosed:
        pass
    except Exception as e:
        print(f"\n[ASR 异常] 接收结果时出错: {e}")


async def start_asr_session(stream_id, anchor_id):
    """启动与阿里云百炼的 WebSocket 连接"""
    try:
        headers = {"Authorization": f"bearer {API_KEY}"}
        ws = await websockets.connect(WS_URL, extra_headers=headers)
        task_id = uuid.uuid4().hex
        run_cmd = {
            "header": {"action": "run-task", "task_id": task_id, "streaming": "duplex"},
            "payload": {
                "task_group": "audio", "task": "asr", "function": "recognition", "model": MODEL,
                "parameters": {
                    "format": AUDIO_FORMAT, "sample_rate": SAMPLE_RATE,
                    "disfluency_removal_enabled": DISFLUENCY_REMOVAL, "language_hints": LANGUAGE_HINTS,
                    "semantic_punctuation_enabled": SEMANTIC_PUNCTUATION, "max_sentence_silence": MAX_SENTENCE_SILENCE,
                    "punctuation_prediction_enabled": PUNCTUATION_PREDICTION,
                    "inverse_text_normalization_enabled": INVERSE_TEXT_NORMALIZATION, "heartbeat": HEARTBEAT
                },
                "input": {}
            }
        }
        if VOCABULARY_ID:
            run_cmd["payload"]["parameters"]["vocabulary_id"] = VOCABULARY_ID

        await ws.send(json.dumps(run_cmd))
        response = await ws.recv()
        start_res = json.loads(response)

        if start_res.get("header", {}).get("event") == "task-started":
            print(f"\n☁️ [ASR] 成功连接阿里云百炼，开始实时识别 (Stream: {stream_id})")
            recv_task = asyncio.create_task(receive_asr_results(ws, anchor_id))
            asr_sessions[stream_id] = {"ws": ws, "task_id": task_id, "recv_task": recv_task}
        else:
            print(f"\n[ASR 错误] 启动任务失败: {start_res}")
            await ws.close()
    except Exception as e:
        print(f"\n[ASR 启动异常] {e}")


async def stop_asr_session(stream_id):
    """停止 ASR 会话并发送结束指令"""
    session = asr_sessions.get(stream_id)
    if session:
        ws = session["ws"]
        task_id = session["task_id"]
        try:
            if ws.open:
                finish_cmd = {
                    "header": {"action": "finish-task", "task_id": task_id, "streaming": "duplex"},
                    "payload": {"input": {}}
                }
                await ws.send(json.dumps(finish_cmd))
                await asyncio.sleep(0.5)
                await ws.close()
        except Exception as e:
            print(f"\n[ASR 关闭异常] {e}")
        finally:
            session["recv_task"].cancel()
            del asr_sessions[stream_id]


# --- 原有业务逻辑 ---
async def broadcast_live_list():
    live_anchors = list(connections["live"].keys())
    event_payload = format_event("getLiveList", {"anchorIds": live_anchors})
    for admin_id, ws_list in connections["admin"].items():
        for ws in ws_list:
            if not ws.closed:
                try:
                    await ws.send_json(event_payload)
                except Exception as e:
                    print(f"\n[广播错误] 发送在线列表给管理员 {admin_id} 失败: {e}")


async def broadcast_to_anchor(anchor_id, event_name, data):
    ws = connections["live"].get(anchor_id)
    if ws and not ws.closed:
        await ws.send_json(format_event(event_name, data))
        return True
    return False


async def handle_upstream_msg(ws, client_type, anchor_id, token, payload):
    event = payload.get("event")
    req_id = payload.get("requestId")
    data = payload.get("data", {})

    if event == "pong":
        pass
    elif event == "push":
        target_anchor = data.get("anchorId")
        content = data.get("content")
        msg_source = "ai" if token == "AI_API_KEY" else "manual"

        msg_item = save_and_clean_history(target_anchor, {
            "source": msg_source,
            "content": content
        })
        success = await broadcast_to_anchor(target_anchor, "recentMessages", {"messages": [msg_item]})
        if success:
            resp = format_response(ok=True, code="OK", message="success", data={"msgId": msg_item["msgId"]},
                                   request_id=req_id)
        else:
            resp = format_response(ok=False, code="ANCHOR_OFFLINE", message="Anchor is not connected", data=None,
                                   request_id=req_id)
        await ws.send_json(resp)
        print(
            f"\n[消息推送] {client_type}:{anchor_id}({msg_source}) -> 主播:{target_anchor} | 状态: {'成功' if success else '失败'}")
    elif event == "sendSound":
        print(f"\n[语音转写] 主播 {anchor_id}: {data.get('text')} (置信度: {data.get('sttConfidence')})")
        await ws.send_json(format_response(request_id=req_id))
    elif event == "sendChat":
        print(f"\n[对话请求] 主播 {anchor_id} 询问 Agent: {data.get('text')}")
        await asyncio.sleep(1)
        ai_msg = save_and_clean_history(anchor_id, {"source": "ai",
                                                    "content": f"关于 '{data.get('text')}'，建议您可以从价格优势切入。"})
        await broadcast_to_anchor(anchor_id, "recentMessages", {"messages": [ai_msg]})
        await ws.send_json(format_response(request_id=req_id))


# --- 路由处理器 ---
async def ws_handler(request):
    client_type = request.query.get('clientType')
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
            return web.Response(status=401, text="Unauthorized: Invalid Token")

    ws = web.WebSocketResponse()
    await ws.prepare(request)

    if client_type in ["live", "liveMic"]:
        if anchor_id in connections[client_type]:
            old_ws = connections[client_type][anchor_id]
            await old_ws.close(code=4001, message=b'REPLACED_BY_NEW_CONNECTION')
        connections[client_type][anchor_id] = ws
        print(f"\n[连接] {client_type} 端已接入: {anchor_id}")
        if client_type == "live":
            await broadcast_live_list()
    else:
        if anchor_id not in connections[client_type]:
            connections[client_type][anchor_id] = []
        connections[client_type][anchor_id].append(ws)

    if client_type != "liveMic":
        history = message_history.get(anchor_id, [])
        await ws.send_json(format_event("allMessages", {"messages": history}))
        if client_type == "admin":
            await ws.send_json(format_event("getLiveList", {"anchorIds": list(connections["live"].keys())}))

    expecting_binary = False
    current_stream_id = ""
    current_seq = 0
    audio_bytes_received = 0

    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                try:
                    payload = json.loads(msg.data)
                    if client_type == "liveMic":
                        event = payload.get("event")
                        data_payload = payload.get("data", {})

                        if event == "streamStart":
                            current_stream_id = data_payload.get("streamId")
                            print(f"\n🎤 [麦克风] {anchor_id} 开始音频推流 (ID: {current_stream_id})")
                            audio_data_store[current_stream_id] = bytearray()
                            audio_bytes_received = 0

                            # 启动云端 ASR 识别
                            await start_asr_session(current_stream_id, anchor_id)

                        elif event == "streamChunk":
                            expecting_binary = True
                            current_seq = data_payload.get("seq", 0)

                        elif event == "streamEnd":
                            s_id = data_payload.get("streamId") or current_stream_id
                            if s_id in audio_data_store:
                                save_wav_file(s_id, audio_data_store[s_id])
                                del audio_data_store[s_id]
                            print(f"\n🛑 [麦克风] {anchor_id} 结束音频推流。")

                            # 结束云端 ASR 识别
                            await stop_asr_session(s_id)

                    else:
                        await handle_upstream_msg(ws, client_type, anchor_id, token, payload)
                except Exception as e:
                    print(f"\n[异常] 解析文本消息失败: {e}")

            elif msg.type == web.WSMsgType.BINARY:
                if client_type == "liveMic" and expecting_binary:
                    if current_stream_id in audio_data_store:
                        audio_data_store[current_stream_id].extend(msg.data)

                        # 将二进制音频实时发送给阿里云百炼 ASR
                        if current_stream_id in asr_sessions:
                            asr_ws = asr_sessions[current_stream_id]["ws"]
                            if asr_ws.open:
                                try:
                                    await asr_ws.send(msg.data)
                                except Exception as e:
                                    print(f"\n[ASR 发送异常] {e}")

                    audio_bytes_received += len(msg.data)
                    expecting_binary = False

                    # 屏蔽了原本每次 seq%50 时的打印，防止打断 ASR 识别结果的同行输出
                    # if current_seq % 50 == 0:
                    #     pass
                else:
                    print(f"\n[警告] 收到意外的二进制数据")

    finally:
        # 清理
        if client_type == "liveMic" and current_stream_id:
            await stop_asr_session(current_stream_id)

        if client_type in ["live", "liveMic"]:
            if connections[client_type].get(anchor_id) == ws:
                del connections[client_type][anchor_id]
                if client_type == "live":
                    await broadcast_live_list()
        else:
            if ws in connections[client_type].get(anchor_id, []):
                connections[client_type][anchor_id].remove(ws)
        print(f"\n[断开] {client_type} 端已离开: {anchor_id}")

    return ws


# --- 后台任务 ---
async def heartbeat_task(app):
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL)
        ping_pkt = format_event("ping", {"nonce": str(uuid.uuid4())[:8]})
        for c_type in connections:
            if c_type in ["live", "liveMic"]:
                for ws in connections[c_type].values():
                    if not ws.closed: await ws.send_json(ping_pkt)
            else:
                for ws_list in connections[c_type].values():
                    for ws in ws_list:
                        if not ws.closed: await ws.send_json(ping_pkt)


async def simulate_ai_broadcast(app):
    while True:
        await asyncio.sleep(60)
        for anchor_id in list(connections["live"].keys()):
            msg_item = save_and_clean_history(anchor_id, {
                "source": "ai",
                "content": f"【AI 建议】{random.randint(1000, 9999)}({time.strftime('%H:%M:%S')})"
            })
            await broadcast_to_anchor(anchor_id, "recentMessages", {"messages": [msg_item]})


# --- App 启动与关闭 ---
async def on_startup(app):
    print("[系统] 创建音频临时目录...")
    if not os.path.exists(RECORDINGS_DIR):
        os.makedirs(RECORDINGS_DIR)

    app['heartbeat'] = asyncio.create_task(heartbeat_task(app))
    app['ai_sim'] = asyncio.create_task(simulate_ai_broadcast(app))


async def on_cleanup(app):
    print("\n[系统] 正在关闭任务并清理音频文件...")
    app['heartbeat'].cancel()
    app['ai_sim'].cancel()

    if os.path.exists(RECORDINGS_DIR):
        shutil.rmtree(RECORDINGS_DIR)
        print(f"[系统] 已删除 {RECORDINGS_DIR} 目录下的所有音频")


app = web.Application()
app.router.add_get('/ws', ws_handler)

cors = aiohttp_cors.setup(app, defaults={
    "*": aiohttp_cors.ResourceOptions(allow_credentials=True, expose_headers="*", allow_headers="*")
})
for route in list(app.router.routes()):
    cors.add(route)

app.on_startup.append(on_startup)
app.on_cleanup.append(on_cleanup)

if __name__ == '__main__':
    print(f"==================================================")
    print(f"  Content Agent 模拟服务器 (融合阿里云实时流 ASR)")
    print(f"  音频文件将保存在: ./{RECORDINGS_DIR}/")
    print(f"  退出程序时将自动清空该目录")
    print(f"==================================================")
    web.run_app(app, port=PORT, access_log=None)