"""
M4 · 全局配置（模型可替换的单一入口）
====================================
设计要点
--------
- 把「用哪个模型 / 密钥 / 知识库路径」集中到一处。开发用免费 Agnes，比赛切付费
  DeepSeek 时**只改 .env，零代码改动**。
- LLMAdapter.from_env() 已支持 agnes / deepseek / ollama 三套；本模块把它收敛成统一配置解析。
- 比赛切换示例（改 .env 即可，无需碰代码）：
      LLM_PROVIDER=deepseek
      DEEPSEEK_API_KEY=sk-xxxx
      DEEPSEEK_MODEL=deepseek-chat
"""
from __future__ import annotations

import os

PROVIDER_DEFAULTS = {
    "agnes": {
        "name": "agnes",
        "base_url": "https://api.agnes-ai.cn/v1",
        "api_key_env": "AGNES_API_KEY",
        "model_env": "AGNES_MODEL",
        "model_default": "agnes-2.5-flash",
        "api_key_default": "",
    },
    "deepseek": {
        "name": "deepseek",
        "base_url": "https://api.deepseek.com",
        "api_key_env": "DEEPSEEK_API_KEY",
        "model_env": "DEEPSEEK_MODEL",
        "model_default": "deepseek-chat",
        "api_key_default": "",
    },
    "ollama": {
        "name": "ollama",
        "base_url": "http://localhost:11434/v1",
        "api_key_env": "OLLAMA_API_KEY",
        "model_env": "OLLAMA_MODEL",
        "model_default": "qwen2.5:7b",
        "api_key_default": "ollama",  # 本地 ollama 不需要真实密钥
    },
}


def get_provider_config(provider: str | None = None) -> dict:
    """解析指定（或环境变量 LLM_PROVIDER）的模型配置。

    返回 {name, base_url, api_key, model}，对应 OpenAICompatProvider 构造参数。
    """
    provider = (provider or os.getenv("LLM_PROVIDER", "ollama")).lower()
    spec = PROVIDER_DEFAULTS.get(provider, PROVIDER_DEFAULTS["ollama"])
    base_url = os.getenv(f"{provider.upper()}_BASE_URL", spec["base_url"])
    api_key = os.getenv(spec["api_key_env"], spec.get("api_key_default", ""))
    model = os.getenv(spec["model_env"], spec["model_default"])
    return {"name": spec["name"], "base_url": base_url, "api_key": api_key, "model": model}


def knowledge_dir() -> str:
    """知识库目录（默认 knowledge/，可用环境变量覆盖）。"""
    return os.getenv("KNOWLEDGE_DIR", "knowledge")


# ---------------------------------------------------- M9 · 发票 OCR（视觉模型）
def get_ocr_config() -> dict:
    """M9 发票 OCR 配置：默认复用 DeepSeek 云端视觉模型（deepseek-flash 原生支持图片输入）。

    环境变量（均可覆盖，未配置时回落 DeepSeek 凭证，做到「填一个 key 即用」）：
      OCR_BASE_URL   默认取 DEEPSEEK_BASE_URL（https://api.deepseek.com）
      OCR_API_KEY    默认取 DEEPSEEK_API_KEY
      OCR_MODEL      默认 deepseek-flash（legacy: deepseek-v4-flash-vision-exp 自动路由）
      OCR_DETAIL     图片精度 high/original/low，默认 high（发票文字保真）
      OCR_MAX_IMAGE_MB   原始图片字节上限，默认 8
    """
    return {
        "name": os.getenv("OCR_PROVIDER", "deepseek-ocr"),
        "base_url": os.getenv("OCR_BASE_URL", os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")),
        "api_key": os.getenv("OCR_API_KEY", os.getenv("DEEPSEEK_API_KEY", "")),
        "model": os.getenv("OCR_MODEL", os.getenv("DEEPSEEK_VL_MODEL", "deepseek-flash")),
        "detail": os.getenv("OCR_DETAIL", "high"),
        "max_image_mb": int(os.getenv("OCR_MAX_IMAGE_MB", "8")),
    }
