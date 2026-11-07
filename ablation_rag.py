"""
消融实验：验证两个创新点的有效性。

2×2 设计：
  ┌──────────┬──────────────┬──────────────────┐
  │          │ 旧版 Chunk   │ 新版 Chunk        │
  ├──────────┼──────────────┼──────────────────┤
  │ 纯 Dense │ A (v2+dense) │ B (v3+dense)     │
  ├──────────┼──────────────┼──────────────────┤
  │ 混合检索 │ C (v2+hybrid)│ D (v3+hybrid)    │ ← 完整系统
  └──────────┴──────────────┴──────────────────┘

指标：Recall@5 / Precision@5 / MRR@5

运行：python ablation_rag.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from backend.rag.ingest import ingest_documents
from backend.rag.retrieve import search_knowledge_tool

EVAL_PATH = Path("data/eval/eval_cases.json")
CACHE_DIR = Path(__file__).resolve().parent / "data" / "eval" / "retrieval"

# 评测集 policy_id → doc_id 映射
POLICY_TO_DOCID = {
    "RETURN-2026-V1": {"KB-AFTERSALE-004"},
    "EXCHANGE-2026-V1": {"KB-AFTERSALE-004"},
    "AFTERSALE-2026-V1": {"KB-AFTERSALE-004"},
    "SERVICE-2026-V1": {"KB-AFTERSALE-004"},
    "SHIP-2026-V1": {"KB-ORDER-003"},
    "CANCEL-2026-V1": {"KB-ORDER-003"},
    "ORDER-2026-V1": {"KB-ORDER-003"},
    "PAYMENT-2026-V1": {"KB-ORDER-003", "KB-STORE-001"},
    "PRODUCT-2026-V1": {"KB-CATALOG-002", "KB-TECH-005"},
    "PRIVACY-2026-V1": {"KB-STORE-001"},
}

CONFIGS = [
    ("A: 旧Chunk + 纯Dense",   "chunks_v2_customer.json", "dense",  "abl_v2_dense"),
    ("B: 新Chunk + 纯Dense",   "chunks_v3_customer.json", "dense",  "abl_v3_dense"),
    ("C: 旧Chunk + 混合检索",  "chunks_v2_customer.json", "hybrid", "abl_v2_hybrid"),
    ("D: 新Chunk + 混合检索",  "chunks_v3_customer.json", "hybrid", "abl_v3_hybrid"),
]


def load_eval_cases():
    with open(EVAL_PATH, encoding="utf-8") as f:
        cases = json.load(f)
    return [c for c in cases if c.get("category") == "rag" and c.get("expected_policy_id")]


def is_relevant(chunk, expected_policy_id):
    return chunk.get("doc_id") in POLICY_TO_DOCID.get(expected_policy_id, set())


def evaluate(cases, search_mode, collection_name, top_k=5):
    recalls, precisions, rrs = [], [], []
    for case in cases:
        chunks = search_knowledge_tool(
            case["user_query"], audience="customer", top_k=top_k,
            search_mode=search_mode, collection_name=collection_name, truncate=0,
        )
        relevant_flags = [is_relevant(c, case["expected_policy_id"]) for c in chunks]
        # Recall
        recalls.append(1.0 if any(relevant_flags) else 0.0)
        # Precision
        precisions.append(sum(relevant_flags) / len(chunks) if chunks else 0.0)
        # MRR
        rr = 0.0
        for rank, hit in enumerate(relevant_flags, start=1):
            if hit:
                rr = 1.0 / rank
                break
        rrs.append(rr)
    return {
        "recall": sum(recalls) / len(recalls),
        "precision": sum(precisions) / len(precisions),
        "mrr": sum(rrs) / len(rrs),
    }


def main():
    cases = load_eval_cases()
    print(f"评测用例数: {len(cases)}")

    # Step 1: 加载 chunks 并入库到各自的 collection
    print("\n=== 入库 ===")
    for name, cache_file, search_mode, coll in CONFIGS:
        chunks_path = CACHE_DIR / cache_file
        with open(chunks_path, encoding="utf-8") as f:
            chunks = json.load(f)
        n = ingest_documents(audience="customer", rebuild=True,
                             collection_name=coll, chunks=chunks)
        print(f"  {name}: {len(chunks)} chunks → {coll} (写入 {n})")

    # Step 2: 评估
    print("\n=== 评估结果 ===")
    results = {}
    for name, cache_file, search_mode, coll in CONFIGS:
        metrics = evaluate(cases, search_mode, coll)
        results[name] = metrics
        print(f"\n  {name}")
        print(f"    Recall@5    = {metrics['recall']:.2%}")
        print(f"    Precision@5 = {metrics['precision']:.2%}")
        print(f"    MRR@5       = {metrics['mrr']:.3f}")

    # Step 3: 输出对比表
    print("\n" + "=" * 70)
    print(f"{'配置':<28} {'Recall@5':>10} {'Precision@5':>13} {'MRR@5':>10}")
    print("-" * 70)
    for name, m in results.items():
        print(f"{name:<28} {m['recall']:>9.2%} {m['precision']:>12.2%} {m['mrr']:>10.3f}")

    # Step 4: 增量分析
    print("\n=== 增量分析 ===")
    a, b, c, d = (results[n] for n, *_ in CONFIGS)

    # 创新点1：Chunk 切分优化的影响（固定检索方式，看 v2→v3 的提升）
    dense_gain = {
        "recall": b["recall"] - a["recall"],
        "precision": b["precision"] - a["precision"],
        "mrr": b["mrr"] - a["mrr"],
    }
    hybrid_gain = {
        "recall": d["recall"] - c["recall"],
        "precision": d["precision"] - c["precision"],
        "mrr": d["mrr"] - c["mrr"],
    }
    print("  【创新点1：Chunk 切分优化（旧→新）】")
    print(f"    纯Dense下增益:  Recall +{dense_gain['recall']:.2%}  Precision +{dense_gain['precision']:.2%}  MRR +{dense_gain['mrr']:.3f}")
    print(f"    混合检索下增益: Recall +{hybrid_gain['recall']:.2%}  Precision +{hybrid_gain['precision']:.2%}  MRR +{hybrid_gain['mrr']:.3f}")

    # 创新点2：混合检索的影响（固定 chunk 版本，看 dense→hybrid 的提升）
    v2_gain = {
        "recall": c["recall"] - a["recall"],
        "precision": c["precision"] - a["precision"],
        "mrr": c["mrr"] - a["mrr"],
    }
    v3_gain = {
        "recall": d["recall"] - b["recall"],
        "precision": d["precision"] - b["precision"],
        "mrr": d["mrr"] - b["mrr"],
    }
    print("  【创新点2：Dense+BM25 混合检索（纯向量→混合）】")
    print(f"    旧Chunk下增益:  Recall +{v2_gain['recall']:.2%}  Precision +{v2_gain['precision']:.2%}  MRR +{v2_gain['mrr']:.3f}")
    print(f"    新Chunk下增益:  Recall +{v3_gain['recall']:.2%}  Precision +{v3_gain['precision']:.2%}  MRR +{v3_gain['mrr']:.3f}")

    # 完整系统 vs 最弱基线
    print(f"\n  【完整系统(D) vs 最弱基线(A)】")
    print(f"    Recall    {a['recall']:.2%} → {d['recall']:.2%} (+{d['recall']-a['recall']:.2%})")
    print(f"    Precision {a['precision']:.2%} → {d['precision']:.2%} (+{d['precision']-a['precision']:.2%})")
    print(f"    MRR       {a['mrr']:.3f} → {d['mrr']:.3f} (+{d['mrr']-a['mrr']:.3f})")


if __name__ == "__main__":
    main()
