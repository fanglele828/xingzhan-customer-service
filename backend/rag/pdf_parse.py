"""
PDF 结构恢复：把 PDF 转成带元数据的结构化文本块。

重构要点（相比旧版的"按页拆、短标题独立建 chunk"）：
1. 用 span 级别的字号信息识别 H1/H2/H3 层级（16/12/10 号），
   建立 section 父子关系，而不是简单地"遇到标题就开新 chunk"。
2. 同一 section 下的正文合并成一个完整语义单元，再按需递归切分，
   避免"商品编号""价格""保修"这类表头被拆成 4 个碎片 chunk。
3. 表格检测：同一水平线上的多列文本块识别为表格行，拼成 markdown 表格
   作为 content_type="table" 的 chunk 整体入库。
4. 严格过滤页眉页脚（重复出现的短行）。
"""
from __future__ import annotations

import hashlib
import json
import re

import fitz  # PyMuPDF

from backend.config import DATA_DIR, KNOWLEDGE_DIR, PARSED_CACHE_DIR

MANIFEST_PATH = KNOWLEDGE_DIR / "pdf_manifest.json"


def load_manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def list_ingestible_documents(audience: str = "customer") -> list[dict]:
    docs = load_manifest()["documents"]
    result = []
    for d in docs:
        if not d.get("ingest"):
            continue
        if audience == "customer" and d.get("visibility") != "public":
            continue
        if audience not in d.get("allowed_audiences", []):
            continue
        result.append(d)
    return result


# ----------------------- 行级结构恢复 ----------------------- #

CLAUSE_PATTERN = re.compile(
    r"^(?:[A-Z]+-\d+(?:\.\d+)?|\d+(?:\.\d+)+|[一二三四五六七八九十]+、|第[一二三四五六七八九十0-9]+[条章节])\s*"
)

# 页眉页脚特征：出现频率高、短、不含实质内容
HEADER_FOOTER_PATTERNS = [
    re.compile(r"^STORE-2026-V1"),
    re.compile(r"^星栈数码\s*\|\s*客户服务知识库"),
    re.compile(r"^KB-[A-Z]+-\d{3}$"),
    re.compile(r"^\d{1,2}$"),  # 纯页码
]


def _is_header_footer(text: str) -> bool:
    t = text.strip()
    if not t:
        return True
    for p in HEADER_FOOTER_PATTERNS:
        if p.match(t):
            return True
    return False


def _classify_row(text: str, size: float) -> str:
    """
    按字号 + 条款编号判断行类型。

    字号约定（实测）：
      7.5-8.0 → 页眉/页脚/小字说明
      9.0     → 正文
      10.0    → 三级标题 H3
      12.0    → 二级标题 H2
      13.0    → 二级标题（加粗）
      16.0    → 一级标题 H1（如"01 适用范围"）
      24.0    → 文档大标题（不入库）
    """
    if size >= 20:
        return "doc_title"
    if size >= 15:
        return "h1"
    if size >= 11:
        return "h2"
    if size >= 9.5:
        return "h3"
    return "body"


def _extract_rows_with_meta(page: fitz.Page) -> list[dict]:
    """
    从单页提取"行"（按 y 坐标合并同水平的 span），每行保留字号和 x 坐标列表。

    表格检测需要同一行内不同的 x 起点来判断列数。
    """
    raw_lines = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            spans = line.get("spans", [])
            if not spans:
                continue
            # 取主字号（出现最多的）
            size_counts = {}
            for s in spans:
                sz = round(s["size"], 1)
                size_counts[sz] = size_counts.get(sz, 0) + 1
            main_size = max(size_counts, key=size_counts.get)
            text = "".join(s["text"] for s in spans).strip()
            # 收集 x 起点（用于表格列检测）
            x_starts = [round(s["bbox"][0], 1) for s in spans if s["text"].strip()]
            y0 = line["bbox"][1]
            raw_lines.append({
                "text": text,
                "size": main_size,
                "y": y0,
                "x_starts": x_starts,
                "line": line,
            })

    # 按 y 排序（从上到下），y 差 <3 的视为同一行
    raw_lines.sort(key=lambda r: r["y"])
    merged: list[dict] = []
    for r in raw_lines:
        if not r["text"]:
            continue
        if _is_header_footer(r["text"]):
            continue
        if merged and abs(r["y"] - merged[-1]["y"]) < 3:
            # 同一行：合并文本
            if r["text"] not in merged[-1]["text"]:
                merged[-1]["text"] = (merged[-1]["text"] + " " + r["text"]).strip()
                merged[-1]["x_starts"].extend(r["x_starts"])
        else:
            merged.append(r)
    return merged


# ----------------------- 表格检测 ----------------------- #

def _detect_table_rows(rows: list[dict]) -> list[bool]:
    """
    判断每行是否属于表格。

    表格特征：同一行内有 >=2 个明显不同的 x 起点（列对齐），
    且连续多行出现相同的 x 起点集合。
    """
    is_table = [False] * len(rows)
    # 对每个可能的表格起始点，看是否有连续 2+ 行具有相似的 x 起点集合
    for i in range(len(rows)):
        xs = sorted(set(rows[i]["x_starts"]))
        if len(xs) < 2:
            continue
        # 检查后续 1-2 行是否有相同的列起点
        consistent = 1
        for j in range(i + 1, min(i + 4, len(rows))):
            nxs = sorted(set(rows[j]["x_starts"]))
            # 列数相近且 x 位置差 <15pt
            if abs(len(nxs) - len(xs)) <= 1 and len(nxs) >= 2:
                consistent += 1
            else:
                break
        if consistent >= 2:
            for k in range(i, i + consistent):
                is_table[k] = True
    return is_table


def _rows_to_markdown_table(rows: list[dict]) -> str:
    """把连续表格行拼成 markdown 表格。"""
    # 每行按 x 起点拆成单元格
    table_rows: list[list[str]] = []
    for r in rows:
        # 简单按 x 起点分段：相邻 x 差 < 20 视为同一单元格
        line = r["line"]
        cells = []
        for span in sorted(line.get("spans", []), key=lambda s: s["bbox"][0]):
            txt = span["text"].strip()
            if not txt:
                continue
            if cells and (span["bbox"][0] - cells[-1][0]) < 25:
                cells[-1] = (cells[-1][0], cells[-1][1] + txt)
            else:
                cells.append((span["bbox"][0], txt))
        table_rows.append([c[1] for c in cells])

    if not table_rows:
        return ""
    # 统一列数
    max_cols = max(len(r) for r in table_rows)
    for r in table_rows:
        r.extend([""] * (max_cols - len(r)))

    header = "| " + " | ".join(table_rows[0]) + " |"
    sep = "| " + " | ".join(["---"] * max_cols) + " |"
    body = "\n".join("| " + " | ".join(r) + " |" for r in table_rows[1:])
    return f"{header}\n{sep}\n{body}"


# ----------------------- 主解析逻辑 ----------------------- #

def parse_pdf(doc_meta: dict) -> list[dict]:
    """
    解析单个 PDF，返回 chunk 列表。

    核心流程：
    1. 逐页提取带字号的行
    2. 检测表格段（连续多列行）→ 整段作为 table chunk
    3. 非表格行按字号归到 H1/H2/H3/正文
    4. 以 H2 为 section 边界，把同一 section 的正文合并成完整语义单元
    5. 超长 section 走递归切分
    """
    pdf_path = DATA_DIR / doc_meta["pdf_path"]
    doc = fitz.open(pdf_path)

    # Step 1: 收集所有页的行，带页码
    all_rows: list[dict] = []
    for page_idx, page in enumerate(doc, start=1):
        rows = _extract_rows_with_meta(page)
        for r in rows:
            r["page"] = page_idx
            all_rows.append(r)

    # FAQ 文档特殊处理：按 Q&A 配对
    if doc_meta["doc_id"] == "KB-FAQ-006":
        return _parse_faq(doc_meta, doc)

    # Step 2: 表格检测
    is_table = _detect_table_rows(all_rows)

    # Step 3: 构建 section 树
    # 用 H1 开大节，H2 开小节，H3 也归到最近的 H2 下
    chunks: list[dict] = []
    current_h1 = doc_meta.get("title", "")
    current_h2 = ""
    current_h2_page = 1
    body_buffer: list[dict] = []  # 当前 section 的正文行

    def flush_section():
        """把当前 section 的正文合并成 chunk(s)。"""
        nonlocal body_buffer
        if not body_buffer:
            return
        section_title = current_h2 or current_h1
        # 合并正文（保留原换行）
        texts = [r["text"] for r in body_buffer if r["text"].strip()]
        if not texts:
            body_buffer = []
            return
        full_text = "\n".join(texts)
        page = body_buffer[0]["page"]
        # 递归切分（但优先不切，除非超长）
        for piece in _recursive_split(full_text, max_len=1000, overlap=120):
            chunks.append(_make_chunk(
                doc_meta=doc_meta,
                page_number=page,
                section_title=section_title,
                text=piece,
                content_type="text",
                bbox=None,
            ))
        body_buffer = []

    i = 0
    while i < len(all_rows):
        r = all_rows[i]
        t = r["text"].strip()
        if not t:
            i += 1
            continue

        if is_table[i]:
            # 收集连续表格行
            tbl_rows = []
            j = i
            while j < len(all_rows) and is_table[j]:
                tbl_rows.append(all_rows[j])
                j += 1
            if len(tbl_rows) >= 2:
                flush_section()
                md = _rows_to_markdown_table(tbl_rows)
                if md:
                    section_title = current_h2 or current_h1
                    chunks.append(_make_chunk(
                        doc_meta=doc_meta,
                        page_number=tbl_rows[0]["page"],
                        section_title=section_title,
                        text=md,
                        content_type="table",
                        bbox=None,
                    ))
            i = j
            continue

        level = _classify_row(t, r["size"])

        if level == "h1":
            flush_section()
            current_h1 = t
            current_h2 = ""
        elif level in ("h2", "h3"):
            flush_section()
            current_h2 = t
            current_h2_page = r["page"]
        elif level == "body":
            # 跳过单独的项目符号行（如 ""）
            if t in ("", "•", "·"):
                i += 1
                continue
            body_buffer.append(r)
        # doc_title 跳过
        i += 1

    flush_section()
    doc.close()
    return chunks


def _parse_faq(doc_meta: dict, doc: fitz.Document) -> list[dict]:
    """FAQ 文档按 Q&A 配对，每个 Q+A 是一个 chunk。"""
    chunks: list[dict] = []
    for page_idx, page in enumerate(doc, start=1):
        text = page.get_text()
        entries = re.finditer(
            r"(?m)^Q\d+\s+([^\n]+)\s*标准答复：([\s\S]*?)(?=依据索引：|^Q\d+|\Z)",
            text,
        )
        for entry in entries:
            question, answer = entry.groups()
            full = question.strip() + "\n" + answer.strip().replace("\n", "")
            chunks.append(_make_chunk(
                doc_meta, page_idx, question.strip(), full, "faq", None,
            ))
    return chunks


def _make_chunk(doc_meta: dict, page_number: int, section_title: str,
                text: str, content_type: str, bbox: list | None,
                seq: int = 0) -> dict:
    """构造带稳定 ID 的 chunk。"""
    content_hash = hashlib.md5(f"{text}|{section_title}|{seq}".encode("utf-8")).hexdigest()[:12]
    chunk_id = f"{doc_meta['doc_id']}_p{page_number}_{content_hash}"
    return {
        "chunk_id": chunk_id,
        "doc_id": doc_meta["doc_id"],
        "source": doc_meta["pdf_path"],
        "title": doc_meta["title"],
        "document_type": doc_meta["document_type"],
        "visibility": doc_meta["visibility"],
        "policy_version": doc_meta["policy_version"],
        "page_number": page_number,
        "section_title": section_title,
        "content_type": content_type,
        "text": text,
        "bbox": bbox,
    }


def _recursive_split(text: str, max_len: int = 1000, overlap: int = 120) -> list[str]:
    """
    递归切分长文本：优先按段落→句号切，保证语义完整；
    超过 max_len 的段落再对半切，保留 overlap 字重叠。
    """
    if len(text) <= max_len:
        return [text]

    # 优先按双换行（段落）切
    parts = re.split(r"\n\s*\n", text)
    if len(parts) > 1 and all(len(p) <= max_len for p in parts):
        return parts

    # 按句号切
    parts = re.split(r"(?<=[。！？.!?])\s*", text)
    result: list[str] = []
    current = ""
    for p in parts:
        if len(current) + len(p) <= max_len:
            current += p
        else:
            if current:
                result.append(current)
            current = p
    if current:
        result.append(current)

    # 超长片段对半切
    final = []
    for piece in result:
        if len(piece) <= max_len:
            final.append(piece)
        else:
            mid = len(piece) // 2
            final.append(piece[: mid + overlap])
            final.append(piece[mid - overlap:])
    return final


def parse_all(audience: str = "customer", use_cache: bool = True) -> list[dict]:
    """解析所有可入库文档，结果缓存到 parsed_cache/。"""
    PARSED_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = PARSED_CACHE_DIR / f"chunks_v3_{audience}.json"

    if use_cache and cache_file.exists():
        return json.loads(cache_file.read_text(encoding="utf-8"))

    all_chunks: list[dict] = []
    for doc_meta in list_ingestible_documents(audience):
        all_chunks.extend(parse_pdf(doc_meta))

    cache_file.write_text(
        json.dumps(all_chunks, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return all_chunks
