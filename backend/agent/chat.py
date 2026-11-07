"""
Agent 运行时：编排主 Agent 处理聊天请求。

核心约束（来自 PROJECT_SPEC 4.4 节）：
- 数据流：用户问题 → LLM 选工具 → 工具返回结构化数据 → LLM 消化证据 → LLM 生成自然语言回复。
- 售后结论采用服务端预审结果；政策引用绑定本轮实际命中的原文。
- System Prompt 明确要求 LLM 先取证据、再用自己的话回答，不要暴露内部字段名。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from backend.agent.factory import get_llm
from backend.agent.tools import make_tools
from backend.rag.ingest import search_knowledge
from backend.rag.retrieve import search_knowledge_tool

SYSTEM_PROMPT = """你是星栈数码（一家虚构3C电商）的智能客服。

【工作流程】
1. 先理解用户的问题，判断是否需要调用工具获取事实依据。
2. 需要事实时，调用合适的工具查询（知识库、商品、订单、物流、工单）。
3. 拿到工具返回的结构化数据后，用你自己的话把证据组织成对用户友好的中文回答。

【必须检索知识库的场景】
以下政策类问题，必须先调用 search_knowledge 检索知识库，绝不能凭记忆回答：
- 退货/换货/保修政策、期限、条件
- 发货时间、配送时效、物流规则
- 运费、免邮门槛、配送费用
- 订单取消、退款规则
- 商品使用说明、故障排查
- 任何涉及"几天"、"多久"、"多少钱"、"能不能"的政策问题
检索后基于知识库内容回答，并简短说明来自哪份文档。

【重要规则】
- 商品价格、库存、规格等硬事实必须来自工具查询结果，绝不能凭记忆编造。
- 订单和物流信息只能基于工具返回的真实数据，不能猜测状态。
- 订单状态中文映射：pending_payment→待付款、paid→已付款/待发货、shipped→已发货、
  delivered→已签收、returning→退货中、exchanging→换货中、returned→已退货、
  cancelled→已取消、refunded→已退款。向用户展示时用中文，不要暴露英文状态码。
- 不要在回答中出现内部字段名（如 product_id、status=paid、price_cny）或机器格式（大括号、JSON）。
- 引用知识时，简短说明来自哪份文档即可，不要整段复制知识库原文。
- 如果工具查询不到相关信息，诚实告诉用户"暂时查不到"，不要编造。
- 回答要口语化、简洁，像真实客服一样。

【政策依据】
- 每轮政策咨询必须重新调用 search_knowledge，以用户的完整问题作为首次查询。一般性问题不需要查个人订单。
- 本轮只调用知识检索时，最终回复必须是 JSON 对象，含 chunk_id 和 quote。quote 直接摘录能回答问题的完整原句，含例外条件，不添加标题、页码或解释。优先使用匹配问法的 FAQ 标准答复；证据不足则返回空对象。
- 禁止承诺取件、预约、退款批准或到账。

【选购与推荐】
- 顾客想买、挑选或询问现货，且已说明品类时，先用 search_products 查询本店商品。只有用途而没有预算也能先查。在已确认的硬条件下有货就展示候选，再询问偏好。
- 将口语品类映射为店内品类（例如充电宝对应移动电源）；预算上限填 max_price。将出差、办公等使用场景作为解释选品的背景，不当作产品规格硬筛条件。没有符合硬条件的商品时，直接说明查无匹配，不自行放宽条件。

【比较具体型号】
- 用户比较多个已命名型号的续航、接口等参数时，直接以用户提供的型号逐个调用 get_product_detail 核对详细规格。只有型号不明确时才搜索候选。查不到某项时明确说明。

【订单回复：站在用户角度】
- 用户关心的是"我买的是什么"，不是订单号或商品编号。
- 查询订单后，先说出商品名称（来自 product_name 字段），再说订单状态和物流。
  例如："您的订单是「星梭 M1 Silent 无线办公鼠标」，目前已签收，物流显示..."
- 订单号（ORD-xxxx）只在需要用户核对时简短提及，不要一上来就报订单号。
- 商品列表用 product_name，绝不要用 product_id（如 P018）回答用户。
- 多件商品时逐一列出名称和数量，例如"1 个星梭 M1 鼠标、1 个墨芯充电器"。

【售后申请】
- 用户问"能不能退"时，先查询订单状态和政策，说明依据。
- 用户给出订单号并表达想退货或换货时，包括“想退货”“想无理由退货”，本轮直接调用 apply_after_sales。该工具会检查订单及规则，即使商品已拆封也应由预审决定是否需人工审核。只有在用户确认写入工单时才需要二次确认。
- apply_after_sales 只是预审，不会自动创建工单；预审通过后告知用户"请在弹出的确认框中确认提交"。
- 不要在未经用户确认的情况下说"已为您提交工单"。
"""


# 会话历史内存：thread_id -> [Message]（最多保留最近 20 条，避免上下文过长）
# 生产环境应改用 Redis/数据库持久化
_conversation_store: dict[tuple[str, str], list] = {}
_MAX_HISTORY = 20


def _get_history(user_id: str, thread_id: str | None) -> list:
    """获取会话历史（如果有 thread_id）。"""
    if not thread_id:
        return []
    return _conversation_store.get((user_id, thread_id), [])


def _append_history(user_id: str, thread_id: str | None, messages: list) -> None:
    """追加消息到会话历史，保留最近 _MAX_HISTORY 条。"""
    if not thread_id:
        return
    history = _conversation_store.get((user_id, thread_id), [])
    history.extend(messages)
    # 只保留最近 N 条（用户+助手对），避免上下文爆炸
    if len(history) > _MAX_HISTORY:
        history = history[-_MAX_HISTORY:]
    _conversation_store[user_id, thread_id] = history


def run_agent(message: str, user_id: str, thread_id: str | None = None) -> dict[str, Any]:
    """
    运行主 Agent 处理单条消息。

    返回：
    - answer: LLM 生成的自然语言回答
    - citations: 从工具调用中提取的知识来源
    - products: 从工具调用中提取的商品卡片
    - tool_traces: 工具调用摘要（调试用）

    异常处理：LLM/工具调用失败时返回友好提示，不暴露堆栈。
    """
    # General policy questions can be answered from reviewed public FAQ entries.
    # Requests to act on a particular purchase still go through the account-bound tools.
    if _general_policy_question(message):
        reply = _lookup_faq(message)
        if reply:
            _append_history(user_id, thread_id, [HumanMessage(content=message), AIMessage(content=reply["answer"])])
            return reply

    tools = make_tools(user_id)
    llm = get_llm()

    agent = create_agent(
        model=llm,
        tools=tools,
        system_prompt=SYSTEM_PROMPT,
        name="customer_service_agent",
    )

    # 构造输入：历史消息 + 当前用户消息
    history = _get_history(user_id, thread_id)
    input_messages = history + [HumanMessage(content=message)]

    try:
        result = agent.invoke({"messages": input_messages})
    except Exception as e:
        # LLM 调用失败或工具执行失败，返回友好提示
        print(f"[agent error] {e}")
        return {
            "answer": "抱歉，我遇到了一点问题，请稍后重试或换个方式提问。",
            "citations": [],
            "products": [],
            "tool_traces": [],
        }

    # 从最终状态提取消息
    messages = result["messages"]

    # 最后一条 AIMessage 就是 LLM 的自然语言回答
    answer = ""
    for msg in reversed(messages):
        if isinstance(msg, AIMessage) and msg.content:
            answer = msg.content if isinstance(msg.content, str) else str(msg.content)
            break

    # 提取工具调用痕迹（用于前端调试面板）
    tool_traces = []
    citations = []
    products = []
    action_required = None
    seen_cites: set = set()
    seen_products: set = set()

    evidence = []
    for msg in messages[len(input_messages):]:
        if isinstance(msg, AIMessage) and msg.tool_calls:
            for tc in msg.tool_calls:
                tool_traces.append({
                    "tool": tc["name"],
                    "args": _summarize_args(tc.get("args", {})),
                })
        if isinstance(msg, ToolMessage):
            # 解析工具返回，提取来源和商品
            try:
                content = msg.content
                if isinstance(content, str):
                    data = json.loads(content)
                else:
                    data = content
                _extract_structured_data(data, citations, products, seen_cites, seen_products)
                if msg.name == "search_knowledge" and isinstance(data, list):
                    evidence.extend(data)
                if msg.name == "apply_after_sales" and isinstance(data, dict) and data.get("decision"):
                    answer = _preview_reply(data)
                    action_required = None
                # 检测售后预审结果：带 draft + decision 的就是 apply_after_sales 的返回
                if isinstance(data, dict) and data.get("draft") and data.get("decision"):
                    draft = data["draft"]
                    action_required = {
                        "action_type": "after_sales_confirm",
                        "request_id": draft["request_id"],
                        "payload": {
                            "order_id": draft.get("order_id"),
                            "issue_type": draft.get("issue_type"),
                        },
                    }
            except (json.JSONDecodeError, TypeError):
                pass

    used_tools = {trace["tool"] for trace in tool_traces}
    if used_tools == {"search_knowledge"}:
        answer, citations = _policy_reply(answer, evidence)
    elif not used_tools and re.search(r"政策|无理由|保修|运费|拆封|退货期限|多久.*发货", message):
        # Some model responses skip lookup even when a factual policy is requested.
        try:
            evidence = search_knowledge_tool(message)
            tool_traces.append({"tool": "search_knowledge", "args": {"query": message}})
            selection = llm.invoke([
                ("system", "从资料中选出直接回答问题的完整原句，保留条件和例外。返回 JSON 对象，"
                           "字段为 chunk_id 和 quote。缺乏证据则返回 {}。"),
                ("user", json.dumps({"question": message, "sources": evidence}, ensure_ascii=False)),
            ])
            answer, citations = _policy_reply(selection.content, evidence)
        except Exception:
            logging.getLogger(__name__).exception("Policy evidence lookup failed")
            answer, citations = _policy_reply("", [])

    # 保存本次对话到历史（只保存用户消息和最终 AI 回答，不保存中间工具消息）
    _append_history(user_id, thread_id, [
        HumanMessage(content=message),
        AIMessage(content=answer),
    ])

    return {
        "answer": answer,
        "citations": citations,
        "products": products,
        "action_required": action_required,
        "tool_traces": tool_traces,
    }


def _general_policy_question(message: str) -> bool:
    if re.search(r"ORD-|TKT-|这[笔个单]|那[笔单]|我的订单|帮我查|我要退|帮我退|我要申请|帮我申请|给我申请|帮我办理", message, re.I):
        return False
    topic = re.search(r"退|换货|售后|拆封|保修|质量问题|故障|运费|物流|发货|送达|取消", message)
    return bool(topic and re.search(r"怎么|如何|什么|多久|多少|能否|能不能|可以|是否|政策|条件|吗|？|\?", message))


def _lookup_faq(question: str) -> dict | None:
    """Select a published FAQ entry only when it answers the full question."""
    try:
        chunks = search_knowledge(question, top_k=5, faq_only=True)
        questions = [chunk["section_title"] for chunk in chunks]
        def normalize(text: str) -> str:
            return re.sub(r"[\s？?。]", "", text)
        index = next((i for i, title in enumerate(questions) if normalize(title) == normalize(question)), None)
        if index is None and questions:
            selection = get_llm().invoke([
                ("system", "判断候选问法是否与用户问题同义且涵盖全部条件。仅选择一条，"
                           "返回从1开始的序号；无匹配、用户要求执行操作、依赖前文或询问个人订单时返回0。"),
                ("user", json.dumps({"问题": question, "候选问法": questions}, ensure_ascii=False)),
            ]).content
            if isinstance(selection, str) and selection.strip() in {str(i+1) for i in range(len(questions))}:
                index = int(selection.strip()) - 1
        if index is None:
            return None
        chunk = chunks[index]
        _, separator, answer = chunk["text"].partition("\n")
        if not separator or not answer.strip():
            return None
        citations = []
        _extract_structured_data(chunk, citations, [], set(), set())
        return {"answer": answer.strip(), "citations": citations, "products": [], "action_required": None,
                "tool_traces": [{"tool": "search_knowledge", "args": {"query": question}}]}
    except Exception:
        logging.getLogger(__name__).exception("Public FAQ lookup failed")
        return None


def _policy_reply(selection: str, evidence: list[dict]) -> tuple[str, list]:
    """Only publish a quote and page from the same retrieved passage."""
    try:
        selected = json.loads(selection.strip().removeprefix("```json").removesuffix("```").strip())
    except (ValueError, AttributeError):
        selected = {}
    if isinstance(selected, dict):
        quote = selected.get("quote")
        for chunk in evidence:
            if (chunk.get("chunk_id") == selected.get("chunk_id") and isinstance(quote, str)
                    and quote.strip() and quote in chunk.get("text", "")):
                citations = []
                _extract_structured_data(chunk, citations, [], set(), set())
                return quote.strip(), citations
    return "现有资料不足以核实这个问题，建议联系人工客服确认。", []


def _preview_reply(preview: dict) -> str:
    labels = {
        "eligible_to_apply": "符合申请条件。",
        "needs_human_review": "需要人工审核。",
        "not_eligible_for_auto_return": "不符合普通退货预审条件。",
    }
    parts = [labels[preview["decision"]], *preview.get("reasons", [])]
    if preview.get("draft"):
        parts.append("如需提交工单，请在弹窗中确认。")
    return "".join(parts)


def _summarize_args(args: dict) -> dict:
    """精简工具参数摘要，避免在调试面板暴露过多内容。"""
    summary = {}
    for k, v in args.items():
        if isinstance(v, str) and len(v) > 100:
            summary[k] = v[:100] + "..."
        else:
            summary[k] = v
    return summary


def _extract_structured_data(data: Any, citations: list, products: list,
                             seen_cites: set, seen_products: set) -> None:
    """从工具返回数据中提取来源卡片和商品卡片（去重）。"""
    if isinstance(data, list):
        for item in data:
            _extract_structured_data(item, citations, products, seen_cites, seen_products)
    elif isinstance(data, dict):
        # 知识来源 chunk
        if "doc_id" in data and "page_number" in data:
            key = (data.get("doc_id", ""), data.get("page_number"), data.get("section_title"))
            if key not in seen_cites:
                seen_cites.add(key)
                citations.append({
                    "doc_id": data.get("doc_id", ""),
                    "title": data.get("title", ""),
                    "page_number": data.get("page_number"),
                    "section_title": data.get("section_title"),
                    "content_type": data.get("content_type"),
                })
        # 商品
        elif "product_id" in data and "price_cny" in data:
            pid = data["product_id"]
            if pid not in seen_products:
                seen_products.add(pid)
                products.append({
                    "product_id": pid,
                    "name": data.get("name", ""),
                    "price_cny": data.get("price_cny"),
                    "stock": data.get("stock", 0),
                    "category": data.get("category", ""),
                    "features": data.get("features", ""),
                    "warranty_months": data.get("warranty_months", 0),
                })
