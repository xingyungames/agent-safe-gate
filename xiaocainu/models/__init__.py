from .env import load_env

load_env()  # 加载项目根 .env（若存在），使 FEISHU_*/LLM_* 等环境变量生效

from .llm import (
    LLMAdapter,
    LLMResult,
    LLMProvider,
    MockProvider,
    OpenAICompatProvider,
    _is_rate_limit,
)
from .retriever import (
    Retriever,
    Chunk,
    format_context,
    load_documents,
    split_text,
    split_markdown,
)
from .config import get_provider_config, knowledge_dir
from .agent import XiaoCaiAgent
from .feishu import (
    FeishuBitableClient,
    FeishuError,
    FeishuTransport,
    UrllibTransport,
    REIMBURSEMENT_FIELDS,
    ALLOWED_TYPES,
    build_reimbursement_record,
    public_view,
)

__all__ = [
    "LLMAdapter",
    "LLMResult",
    "LLMProvider",
    "MockProvider",
    "OpenAICompatProvider",
    "_is_rate_limit",
    "Retriever",
    "Chunk",
    "format_context",
    "load_documents",
    "split_text",
    "split_markdown",
    "get_provider_config",
    "knowledge_dir",
    "XiaoCaiAgent",
    "FeishuBitableClient",
    "FeishuError",
    "FeishuTransport",
    "UrllibTransport",
    "REIMBURSEMENT_FIELDS",
    "ALLOWED_TYPES",
    "build_reimbursement_record",
    "public_view",
]
