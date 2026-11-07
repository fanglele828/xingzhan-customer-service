"""
配置模块：从 .env 读取环境变量，统一管理项目运行所需的配置。

设计原则：
- 所有密钥只从环境变量读取，不硬编码。
- 数据路径基于本文件位置解析，不依赖当前工作目录。
- 演示时钟与真实时钟分离：默认走真实时间，评测可注入固定时钟。
"""
from __future__ import annotations

import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

# ----------------------------- 路径常量 ----------------------------- #
# 项目根目录：backend/ 的上一级
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
BUSINESS_DIR = DATA_DIR / "business"
KNOWLEDGE_DIR = DATA_DIR / "knowledge"
RAW_PDF_DIR = KNOWLEDGE_DIR / "raw_pdfs"

# SQLite 业务库与 LangGraph 检查点库分文件存储
SQLITE_DB_PATH = BUSINESS_DIR / "demo_store.sqlite3"
CHECKPOINT_DB_PATH = BUSINESS_DIR / "checkpoint.sqlite3"

# 解析缓存目录（PDF 解析产物，便于重复入库）
PARSED_CACHE_DIR = KNOWLEDGE_DIR / "parsed_cache"

# ----------------------------- 环境变量 ----------------------------- #
load_dotenv(PROJECT_ROOT / ".env")


class Settings:
    """集中管理所有可配置项。"""

    # ---- LLM（通义千问，走 DashScope 兼容 OpenAI 接口） ---- #
    LLM_MODEL: str = os.getenv("LLM_MODEL", "qwen3.7-plus")
    DASHSCOPE_API_KEY: str = os.getenv("DASHSCOPE_API_KEY", "")
    DASHSCOPE_BASE_URL: str = os.getenv(
        "DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
    )

    # ---- Embedding（硅基流动 BGE-M3，1024 维） ---- #
    EMBEDDING_MODEL: str = os.getenv("EMBEDDING_MODEL", "BAAI/bge-m3")
    SILICONFLOW_API_KEY: str = os.getenv("SILICONFLOW_API_KEY", "")
    SILICONFLOW_BASE_URL: str = os.getenv(
        "SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1"
    )
    EMBEDDING_DIM: int = 1024  # BGE-M3 固定维度

    # ---- Milvus 向量库 ---- #
    MILVUS_URI: str = os.getenv("MILVUS_URI", "http://localhost:19530")
    MILVUS_COLLECTION: str = "xingzhan_knowledge_v3"

    # ---- 业务规则 ---- #
    POLICY_VERSION: str = "STORE-2026-V1"
    TIMEZONE = timezone(timedelta(hours=8))  # Asia/Shanghai

    # ---- 演示时钟 ---- #
    # 评测基准日 2026-11-04；正常运行使用真实时间。
    # 如需固定演示时钟，设置环境变量 FIXED_NOW=2026-11-04T00:00:00+08:00
    _fixed_now: Optional[datetime] = None

    @classmethod
    def now(cls) -> datetime:
        """返回当前业务时间。支持通过 FIXED_NOW 注入固定时钟（用于评测）。"""
        if cls._fixed_now is not None:
            return cls._fixed_now
        fixed = os.getenv("FIXED_NOW")
        if fixed:
            cls._fixed_now = datetime.fromisoformat(fixed)
            return cls._fixed_now
        return datetime.now(cls.TIMEZONE)

    @classmethod
    def set_fixed_now(cls, dt: Optional[datetime]) -> None:
        """设置/清除固定时钟，仅用于测试。"""
        cls._fixed_now = dt


settings = Settings()
