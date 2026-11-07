"""
Milvus 向量库入库与检索。

关键设计：
- Collection schema 同时包含向量字段（Dense 检索）和 text 字段（BM25 全文检索），
  实现 Dense + BM25 混合检索。
- visibility 字段用于 metadata filter：顾客侧检索自动排除内部 SOP。
- chunk_id 作为主键，重复入库不会产生重复向量（upsert 语义）。
- 不默认 drop 现有 collection，避免误删用户数据。
"""
from __future__ import annotations

import logging
from typing import Any

from pymilvus import (
    Collection,
    CollectionSchema,
    AnnSearchRequest,
    RRFRanker,
    Function,
    FunctionType,
    DataType,
    FieldSchema,
    connections,
    utility,
)

from backend.config import settings
from backend.rag.pdf_parse import parse_all

# Collection 名称（带版本后缀，便于迁移）
COLLECTION_NAME = settings.MILVUS_COLLECTION


def _connect() -> None:
    """连接 Milvus（已连接则跳过）。"""
    connections.connect(alias="default", uri=settings.MILVUS_URI)


def _get_schema() -> CollectionSchema:
    """定义 Collection schema。"""
    fields = [
        FieldSchema(name="chunk_id", dtype=DataType.VARCHAR, is_primary=True, max_length=128),
        FieldSchema(name="doc_id", dtype=DataType.VARCHAR, max_length=64),
        FieldSchema(name="text", dtype=DataType.VARCHAR, max_length=65535,
                    enable_analyzer=True, analyzer_params={"type": "chinese"},
                    ),
        FieldSchema(name="embedding", dtype=DataType.FLOAT_VECTOR, dim=settings.EMBEDDING_DIM),
        FieldSchema(name="page_number", dtype=DataType.INT32),
        FieldSchema(name="visibility", dtype=DataType.VARCHAR, max_length=32),
        FieldSchema(name="policy_version", dtype=DataType.VARCHAR, max_length=64),
        FieldSchema(name="section_title", dtype=DataType.VARCHAR, max_length=512),
        FieldSchema(name="document_type", dtype=DataType.VARCHAR, max_length=32),
        FieldSchema(name="source", dtype=DataType.VARCHAR, max_length=512),
        FieldSchema(name="title", dtype=DataType.VARCHAR, max_length=256),
        FieldSchema(name="content_type", dtype=DataType.VARCHAR, max_length=32),
    ]
    fields.append(FieldSchema(name="sparse", dtype=DataType.SPARSE_FLOAT_VECTOR))
    bm25 = Function(name="text_bm25", input_field_names=["text"],
                    output_field_names=["sparse"], function_type=FunctionType.BM25)
    return CollectionSchema(fields=fields, functions=[bm25], description="星栈数码知识库")


def create_collection_if_not_exists(collection_name: str | None = None) -> Collection:
    """创建 Collection（如不存在），并建立索引。"""
    _connect()
    name = collection_name or COLLECTION_NAME
    if utility.has_collection(name):
        col = Collection(name)
    else:
        schema = _get_schema()
        col = Collection(name=name, schema=schema)
        # Dense 向量索引：HNSW
        col.create_index(
            field_name="embedding",
            index_params={
                "metric_type": "COSINE",
                "index_type": "HNSW",
                "params": {"M": 16, "efConstruction": 200},
            },
        )
    if not any(index.field_name == "sparse" for index in col.indexes):
        col.create_index("sparse", {"index_type": "SPARSE_INVERTED_INDEX", "metric_type": "BM25"})
    col.load()
    return col


def ingest_documents(audience: str = "customer", rebuild: bool = False,
                     collection_name: str | None = None,
                     chunks: list[dict] | None = None) -> int:
    """
    解析所有允许入库的 PDF 并写入 Milvus。

    - rebuild=False：只 upsert 新的/变化的 chunk（按 chunk_id 去重）。
    - rebuild=True：先清空 Collection 再全量写入（慎用）。
    - collection_name：指定写入的 Milvus collection（消融实验用）。
    - chunks：直接传入已解析好的 chunk 列表（跳过 PDF 解析，消融实验用）。
    返回写入的 chunk 数量。
    """
    from backend.agent.factory import get_embeddings  # 延迟导入避免循环

    col = create_collection_if_not_exists(collection_name)

    if chunks is None:
        chunks = parse_all(audience=audience, use_cache=True)

    if rebuild:
        col.delete(expr="chunk_id != ''")

    embeddings = get_embeddings()
    texts = [c["text"] for c in chunks]
    vectors = embeddings.embed_documents(texts)

    # 分批写入（Milvus 单次写入量有限）
    batch_size = 100
    written = 0
    for i in range(0, len(chunks), batch_size):
        batch = chunks[i:i + batch_size]
        batch_vecs = vectors[i:i + batch_size]
        data = [
            [c["chunk_id"] for c in batch],
            [c["doc_id"] for c in batch],
            [c["text"] for c in batch],
            batch_vecs,
            [c["page_number"] for c in batch],
            [c["visibility"] for c in batch],
            [c["policy_version"] for c in batch],
            [c["section_title"][:500] for c in batch],
            [c["document_type"] for c in batch],
            [c["source"] for c in batch],
            [c["title"] for c in batch],
            [c["content_type"] for c in batch],
        ]
        col.upsert(data)
        written += len(batch)

    col.flush()
    col.load()
    return written


# ----------------------- 检索 ----------------------- #

def search_knowledge(query: str, top_k: int = 5, audience: str = "customer", *,
                     faq_only: bool = False,
                     search_mode: str = "hybrid",
                     collection_name: str | None = None) -> list[dict]:
    """
    检索知识库。

    search_mode:
      - "hybrid": Dense + BM25 + RRFRanker 融合（默认，完整系统）
      - "dense": 仅 Dense 向量检索（消融用）
    """
    from backend.agent.factory import get_embeddings

    _connect()
    col = Collection(collection_name or COLLECTION_NAME)

    query_vec = get_embeddings().embed_query(query)

    # visibility filter
    filters = []
    if audience == "customer" or faq_only:
        filters.append('visibility == "public"')
    filters.append('content_type == "faq"' if faq_only else 'content_type != "faq"')
    visibility_filter = " and ".join(filters)
    fields = ["chunk_id", "doc_id", "text", "page_number", "section_title",
              "document_type", "visibility", "policy_version", "source", "title", "content_type"]
    candidates = max(top_k * 3, 20)

    if search_mode == "dense":
        # 消融组：纯 Dense 向量检索
        results = col.search(
            data=[query_vec],
            anns_field="embedding",
            param={"metric_type": "COSINE", "params": {"ef": max(64, candidates)}},
            limit=top_k,
            expr=visibility_filter,
            output_fields=fields,
        )
        return [{field: hit.entity.get(field) for field in fields} for hit in results[0]]

    # 默认：Dense + BM25 混合检索
    requests = [
        AnnSearchRequest([query_vec], "embedding", {"metric_type": "COSINE", "params": {"ef": max(64, candidates)}},
                         candidates, expr=visibility_filter),
        AnnSearchRequest([query], "sparse", {"metric_type": "BM25"}, candidates, expr=visibility_filter),
    ]
    results = col.hybrid_search(reqs=requests, rerank=RRFRanker(), limit=top_k, output_fields=fields)
    logging.getLogger(__name__).info("Milvus retrieval: dense+bm25, %d hits", len(results[0]))
    return [{field: hit.entity.get(field) for field in fields} for hit in results[0]]


def get_collection_stats() -> dict[str, Any]:
    """返回 Collection 基本信息（用于健康检查）。"""
    _connect()
    if not utility.has_collection(COLLECTION_NAME):
        return {"exists": False, "count": 0}
    col = Collection(COLLECTION_NAME)
    return {"exists": True, "count": col.num_entities, "name": COLLECTION_NAME}
