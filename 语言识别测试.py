import asyncio
import websockets
import json
import uuid
import os
import pyaudio
import sys

# =====================================================================
# ======================== 1. 基础参数配置区 ==========================
# =====================================================================

# 阿里云百炼 API Key (建议通过环境变量读取，也可在此处直接将 None 替换为你的真实 Key 字符串)
API_KEY = 'sk-b90082f1c5544f75891a6d52d2c51559'

# WebSocket 服务地址
WS_URL = "wss://dashscope.aliyuncs.com/api-ws/v1/inference/"

# 终端输出颜色定义 (ANSI 转义码)
COLOR_RED = "\033[91m"
COLOR_RESET = "\033[0m"

# =====================================================================
# ======================== 2. 语音识别参数配置区 ======================
# =====================================================================

# 模型选择 (推荐 paraformer-realtime-v2)
MODEL = "paraformer-realtime-v2"

# 音频格式配置 (麦克风原始数据流通常为 pcm)
AUDIO_FORMAT = "pcm"
SAMPLE_RATE = 16000  # 采样率 (16000Hz 对应 paraformer-realtime-v2 标准)
CHANNELS = 1  # 必须为单声道
CHUNK_SIZE = 1600  # 每次读取的音频帧数 (16000 * 0.1s = 1600帧，即推荐的100ms)

# 识别控制参数
LANGUAGE_HINTS = ["zh", "en"]  # 指定待识别语言代码 (支持 zh, en, ja 等)
DISFLUENCY_REMOVAL = False  # 是否过滤语气词 (嗯、啊等)
SEMANTIC_PUNCTUATION = False  # 语义断句 (True:语义断句准确度高适合会议, False:VAD断句延迟低适合交互)
MAX_SENTENCE_SILENCE = 800  # VAD 断句的静音时长阈值 (单位 ms，仅在 SEMANTIC_PUNCTUATION=False 生效)
PUNCTUATION_PREDICTION = True  # 是否自动添加标点符号
INVERSE_TEXT_NORMALIZATION = True  # 是否开启 ITN (开启后中文数字转为阿拉伯数字)
HEARTBEAT = False  # 是否允许长时间静音不断开连接

# 热词配置 (若没有定制热词，请保持为空字符串)
VOCABULARY_ID = ""

# =====================================================================
# ======================== 3. 核心业务逻辑代码 ========================
# =====================================================================

# 全局运行标识，用于优雅捕获 Ctrl+C 退出
is_running = True


async def send_audio(ws, task_id):
    """
    负责不断从麦克风读取音频并发送到 WebSocket
    """
    global is_running
    p = pyaudio.PyAudio()
    stream = p.open(format=pyaudio.paInt16,
                    channels=CHANNELS,
                    rate=SAMPLE_RATE,
                    input=True,
                    frames_per_buffer=CHUNK_SIZE)

    print("\n====== 🎤 开始录音，请说话... (按 Ctrl+C 结束任务) ======\n")

    try:
        while is_running:
            # 使用 to_thread 防止阻塞 asyncio 的事件循环
            data = await asyncio.to_thread(stream.read, CHUNK_SIZE, False)
            await ws.send(data)
            await asyncio.sleep(0.005)  # 极短让出 CPU 控制权

    except websockets.exceptions.ConnectionClosed:
        pass  # 连接关闭，停止发送
    except Exception as e:
        print(f"\n音频采集异常: {e}")
    finally:
        # 清理音频流
        stream.stop_stream()
        stream.close()
        p.terminate()

        # 录音结束，发送 finish-task 指令
        if ws.open:
            print("\n\n====== 🛑 正在通知服务端结束任务... ======")
            finish_cmd = {
                "header": {
                    "action": "finish-task",
                    "task_id": task_id,
                    "streaming": "duplex"
                },
                "payload": {"input": {}}
            }
            await ws.send(json.dumps(finish_cmd))


async def receive_results(ws):
    """
    负责接收服务端的实时识别结果并进行终端高亮渲染
    """
    try:
        async for message in ws:
            res = json.loads(message)
            event = res.get("header", {}).get("event")

            if event == "result-generated":
                sentence = res.get("payload", {}).get("output", {}).get("sentence", {})
                text = sentence.get("text", "")
                is_end = sentence.get("sentence_end", False)

                if not text:
                    continue

                # 清除当前控制台行并覆盖打印
                sys.stdout.write("\r\033[K")

                if is_end:
                    # ✅ 完整句子：使用红色打印，并且换行
                    sys.stdout.write(f"{COLOR_RED}[最终] {text}{COLOR_RESET}\n")
                    sys.stdout.flush()
                else:
                    # ⏳ 中间结果：普通颜色打印，不换行，实现动态刷新
                    sys.stdout.write(f"[中间] {text}")
                    sys.stdout.flush()

            elif event == "task-finished":
                print("\n====== ✅ 任务已成功结束 ======")
                break

            elif event == "task-failed":
                error_msg = res.get("header", {}).get("error_message", "未知错误")
                print(f"\n====== ❌ 任务失败: {error_msg} ======")
                break

    except websockets.exceptions.ConnectionClosed:
        print("\n与服务器的连接已关闭。")


async def main():
    if not API_KEY or API_KEY == "your_api_key_here":
        print("错误：请在代码中配置 API_KEY，或设置环境变量 DASHSCOPE_API_KEY")
        return

    headers = {"Authorization": f"bearer {API_KEY}"}

    print("正在连接到阿里云百炼服务器...")
    try:
        async with websockets.connect(WS_URL, extra_headers=headers) as ws:
            # 1. 生成 32 位 UUID 并发送 run-task 指令开启任务
            task_id = uuid.uuid4().hex

            run_cmd = {
                "header": {
                    "action": "run-task",
                    "task_id": task_id,
                    "streaming": "duplex"
                },
                "payload": {
                    "task_group": "audio",
                    "task": "asr",
                    "function": "recognition",
                    "model": MODEL,
                    "parameters": {
                        "format": AUDIO_FORMAT,
                        "sample_rate": SAMPLE_RATE,
                        "disfluency_removal_enabled": DISFLUENCY_REMOVAL,
                        "language_hints": LANGUAGE_HINTS,
                        "semantic_punctuation_enabled": SEMANTIC_PUNCTUATION,
                        "max_sentence_silence": MAX_SENTENCE_SILENCE,
                        "punctuation_prediction_enabled": PUNCTUATION_PREDICTION,
                        "inverse_text_normalization_enabled": INVERSE_TEXT_NORMALIZATION,
                        "heartbeat": HEARTBEAT
                    },
                    "input": {}
                }
            }

            # 若配置了热词 ID，则添加到参数中
            if VOCABULARY_ID:
                run_cmd["payload"]["parameters"]["vocabulary_id"] = VOCABULARY_ID

            await ws.send(json.dumps(run_cmd))

            # 2. 等待 task-started 事件
            response = await ws.recv()
            start_res = json.loads(response)
            if start_res.get("header", {}).get("event") != "task-started":
                print("启动任务失败:", start_res)
                return

            print("连接成功！任务已开启。")

            # 3. 并发执行音频发送与结果接收
            send_task = asyncio.create_task(send_audio(ws, task_id))
            recv_task = asyncio.create_task(receive_results(ws))

            # 等待接收任务完成（通常由服务端发送 task-finished 触发）
            await recv_task

    except Exception as e:
        print(f"WebSocket 错误: {e}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # 当你在控制台按下 Ctrl+C 时，优雅地通知协程结束录音，并发送 finish-task
        print("\n\n捕获到终止信号(Ctrl+C)，正在安全退出...")
        is_running = False
        # 为了让 finish-task 能够发送完毕，我们暂不直接退出进程，而是等待主逻辑自然结束。
        # 上述 `send_audio` 协程监听到 `is_running=False` 后会执行清理并发送结束指令。