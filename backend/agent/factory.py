"""
LLM 与 Embedding 模型工厂。

集中管理模型实例，避免在多个模块中重复创建连接。

模型选择：
- LLM：通义千问（通过 LLM_MODEL 配置），通过 DashScope 的 OpenAI 兼容接口调用。
- Embedding：BGE-M3（1024 维），通过硅基流动的 OpenAI 兼容接口调用。

为什么用 OpenAI 兼容接口而不是专门 SDK：
- langchain-openai 的 ChatOpenAI / OpenAIEmbeddings 已支持自定义 base_url，
  一套代码兼容多家供应商，切换成本低。
"""
from __future__ import annotations

from functools import lru_cache

from langchain_openai import ChatOpenAI, OpenAIEmbeddings

from backend.config import settings


@lru_cache(maxsize=1)
def get_llm() -> ChatOpenAI:
    """获取通义千问 LLM 实例（单例缓存）。"""
    return ChatOpenAI(
        model=settings.LLM_MODEL,
        api_key=settings.DASHSCOPE_API_KEY,
        base_url=settings.DASHSCOPE_BASE_URL,
        temperature=0.3,
        timeout=60,
    )


@lru_cache(maxsize=1)
def get_embeddings() -> OpenAIEmbeddings:
    """获取 BGE-M3 嵌入模型实例（单例缓存）。"""
    return OpenAIEmbeddings(
        model=settings.EMBEDDING_MODEL,
        api_key=settings.SILICONFLOW_API_KEY,
        base_url=settings.SILICONFLOW_BASE_URL,
        check_embedding_ctx_length=False,
        chunk_size=32,
    )
