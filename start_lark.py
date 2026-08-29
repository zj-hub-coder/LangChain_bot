"""飞书智能问答机器人 —— WebSocket 长连接模式入口。

启动方式：
    python start_lark.py

说明：
- 使用飞书 lark.ws.Client 长连接，无需公网 URL / 内网穿透。
- 消息处理委托给 LarkBot（卡片/流式/三级降级），自身只负责事件分发。
- Agent 能力由 langchain + MCP 工具 + 内置搜索提供，异步处理。
"""

import asyncio
import json
import logging
import threading

import lark_oapi as lark
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

from config import get_settings
from lark_bot import LarkBot
from session_manager import SessionManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ============================================================
# 1. 初始化配置 & 客户端
# ============================================================
settings = get_settings()
sessions = SessionManager()

lark_client = (
    lark.Client.builder()
    .app_id(settings.lark_app_id)
    .app_secret(settings.lark_app_secret)
    .build()
)

bot = LarkBot(
    lark_client=lark_client,
    sessions=sessions,
    update_interval_ms=settings.lark_card_update_interval_ms,
)


# ============================================================
# 2. 消息事件回调
# ============================================================
def do_p2_im_message_receive_v1(data: P2ImMessageReceiveV1) -> None:
    """接收飞书消息事件。"""
    msg = data.event.message
    sender = data.event.sender

    if msg.message_type != "text":
        return

    try:
        content = json.loads(msg.content)
        question = content.get("text", "").strip()
    except (json.JSONDecodeError, AttributeError):
        return

    if not question:
        return

    user_id = sender.sender_id.open_id if sender.sender_id else "unknown"
    logger.info("收到消息 from %s: %s", user_id, question[:80])

    # 在独立线程中运行异步处理
    threading.Thread(
        target=_run_async,
        args=(bot.handle_question, msg.chat_id, msg.message_id, user_id, question),
        daemon=True,
    ).start()


def _run_async(coro_func, *args):
    """在新线程中运行 async 函数。"""
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(coro_func(*args))
    finally:
        loop.close()


# ============================================================
# 3. 事件处理器 & WebSocket 客户端
# ============================================================
event_handler = (
    lark.EventDispatcherHandler.builder("", "")
    .register_p2_im_message_receive_v1(do_p2_im_message_receive_v1)
    .build()
)

ws_client = lark.ws.Client(
    app_id=settings.lark_app_id,
    app_secret=settings.lark_app_secret,
    event_handler=event_handler,
    log_level=lark.LogLevel.INFO,
)


def main() -> None:
    """启动 WebSocket 长连接。"""
    if not settings.lark_ready:
        logger.error(
            "LARK_APP_ID 或 LARK_APP_SECRET 未设置！请在 .env 中配置"
        )
        return
    if not settings.llm_ready:
        logger.error(
            "LLM 配置不完整！请在 .env 中配置 OPENAI_API_KEY / BASE / MODEL"
        )
        return

    logger.info("Starting Feishu bot (WebSocket mode)...")
    logger.info("App ID: %s", settings.lark_app_id)
    print("✅ 机器人启动成功，等待接收消息...")
    ws_client.start()


if __name__ == "__main__":
    main()
