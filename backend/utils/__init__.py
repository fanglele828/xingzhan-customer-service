"""
统一响应封装与异常处理工具。

设计目标：
- 前端永远收到结构一致的 JSON，不直接解析大模型自由文本作为授权依据。
- 内部错误（堆栈、SQL、密钥）绝不返回给前端。
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class ApiResponse(BaseModel):
    """统一响应外壳。"""

    success: bool
    data: Any | None = None
    error: str | None = None


def ok(data: Any = None) -> dict:
    """成功响应。"""
    return {"success": True, "data": data, "error": None}


def fail(message: str) -> dict:
    """失败响应（只给用户友好的错误信息）。"""
    return {"success": False, "data": None, "error": message}


class BizError(Exception):
    """业务层抛出的可预期错误，会被全局异常处理器转为 4xx 响应。"""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
