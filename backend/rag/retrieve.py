"""
知识检索服务。

把 Milvus 的底层检索包装成 Agent 可调用的工具，并做"必要证据覆盖检查"。

亮点 A 的实现：以退货预审为例，自动检查是否召回了关键政策条款，
缺失时做一次定向补检索，仍不足则标记 needs_human_review。
"""
from __future__ import annotations

from typing import Any

from backend.rag.ingest import search_knowledge


def search_knowledge_tool(query: str, audience: str = "customer",
                          top_k: int = 5, *,
                          search_mode: str = "hybrid",
                          collection_name: str | None = None,
                          truncate: int = 600) -> list[dict[str, Any]]:
    """
    Agent 工具：检索知识库。

    返回的是结构化 chunk（含来源、页码），不是拼接后的自然语言。
    LLM 拿到这些结构化证据后，再用自己的话组织回答。

    消融实验参数：
    - search_mode: "hybrid"（混合）或 "dense"（纯向量）
    - collection_name: 指定 Milvus collection
    - truncate: 文本截断长度（0=不截断，消融评估时设 0 以看完整内容）
    """
    chunks = search_knowledge(query, top_k=top_k, audience=audience,
                              search_mode=search_mode, collection_name=collection_name)
    result = []
    for c in chunks:
        text = c.get("text", "")
        if truncate and len(text) > truncate:
            text = text[:truncate]
        result.append({
            "chunk_id": c.get("chunk_id"),
            "doc_id": c.get("doc_id"),
            "title": c.get("title"),
            "page_number": c.get("page_number"),
            "section_title": c.get("section_title"),
            "content_type": c.get("content_type"),
            "policy_version": c.get("policy_version"),
            "text": text,
        })
    return result


def check_evidence_coverage(chunks: list[dict], required_keywords: list[str]) -> dict[str, Any]:
    """
    必要证据覆盖检查：判断召回结果是否包含必需的关键词/条款。

    返回：
    - covered: 是否全部覆盖
    - missing: 缺失的关键词列表
    - diagnosis: ok / insufficient_evidence
    """
    missing = []
    for kw in required_keywords:
        if not any(kw in (c.get("text", "") + c.get("section_title", "")) for c in chunks):
            missing.append(kw)
    return {
        "covered": len(missing) == 0,
        "missing": missing,
        "diagnosis": "ok" if not missing else "insufficient_evidence",
    }
