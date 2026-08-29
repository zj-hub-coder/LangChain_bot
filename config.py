"""配置管理：从 .env 加载全部运行时配置。

所有配置项均需在 .env 中显式声明，不留硬编码默认值。
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

    # === LLM 配置（OpenAI 兼容接口）===
    openai_api_key: str = Field(..., description="API Key")
    openai_api_base: str = Field(..., description="API Base URL")
    llm_model: str = Field(..., description="模型名")
    llm_temperature: float = Field(..., description="温度参数 0-1")
    llm_max_tokens: int = Field(..., description="最大 token 数")

    # === MCP server 配置文件路径 ===
    mcp_servers_file: str = Field(..., description="MCP server JSON 配置文件路径")

    # === 搜索工具 ===
    search_provider: str = Field(..., description="搜索工具提供商")

    # === 飞书机器人 ===
    lark_app_id: str = Field(..., description="飞书 App ID")
    lark_app_secret: str = Field(..., description="飞书 App Secret")
    lark_card_update_interval_ms: int = Field(
        ..., description="卡片流式更新节流间隔（毫秒）"
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
