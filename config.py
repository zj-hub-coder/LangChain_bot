"""配置管理：从 .env 加载全部运行时配置。

所有配置项均提供默认值，缺失时不会在实例化阶段报错；由 llm_ready /
lark_ready 两个属性在对应入口做运行时校验，让 CLI 与飞书两端解耦
（跑 CLI 无需填飞书配置，跑飞书才需要 LARK_* 三项）。
"""
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # === LLM 配置（OpenAI 兼容接口，CLI 与飞书均必填）===
    openai_api_key: str = Field(default="", description="API Key")
    openai_api_base: str = Field(default="", description="API Base URL")
    llm_model: str = Field(default="", description="模型名")
    llm_temperature: float = Field(default=0.7, description="温度参数 0-1")
    llm_max_tokens: int = Field(default=500, description="最大 token 数")

    # === MCP server 配置文件路径 ===
    mcp_servers_file: str = Field(
        default="mcp_servers.json", description="MCP server JSON 配置文件路径"
    )

    # === 搜索工具 ===
    search_provider: str = Field(default="duckduckgo", description="搜索工具提供商")

    # === Agent 循环控制 ===
    max_tool_rounds: int = Field(
        default=8, description="ReAct 最大工具调用轮数（防死循环）"
    )

    # === 可观测：本地 Trace 落盘 ===
    trace_enabled: bool = Field(default=True, description="是否将每次运行落盘为 JSONL trace")
    trace_dir: str = Field(default="traces", description="trace JSONL 输出目录")
    feedback_dir: str = Field(default="feedback", description="飞书点踩反馈 JSONL 目录")

    # === 可观测：Langfuse（可选，自托管；配齐后 trace 双写上报）===
    langfuse_enabled: bool = Field(default=False, description="是否上报 trace 到 Langfuse")
    langfuse_host: str = Field(default="", description="Langfuse 地址，如 http://localhost:3000")
    langfuse_public_key: str = Field(default="", description="Langfuse Public Key (pk-lf-...)")
    langfuse_secret_key: str = Field(default="", description="Langfuse Secret Key (sk-lf-...)")

    # === 飞书机器人（可选，仅 start_lark.py 需要）===
    lark_app_id: str = Field(default="", description="飞书 App ID")
    lark_app_secret: str = Field(default="", description="飞书 App Secret")
    lark_card_update_interval_ms: int = Field(
        default=800, description="卡片流式更新节流间隔（毫秒）"
    )

    @property
    def llm_ready(self) -> bool:
        return bool(self.openai_api_key and self.openai_api_base and self.llm_model)

    @property
    def lark_ready(self) -> bool:
        return bool(self.lark_app_id and self.lark_app_secret)


@lru_cache
def get_settings() -> Settings:
    return Settings()


@lru_cache
def get_mcp_servers() -> dict:
    path = Path(get_settings().mcp_servers_file)
    if not path.is_file():
        return {}
    import json
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} 内容必须是 JSON 对象（dict）")
    return data
