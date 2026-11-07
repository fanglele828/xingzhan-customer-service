# 系统架构

## 一次对话的数据流

```mermaid
flowchart TD
    UI["React 聊天窗口 · 演示用户"] -->|POST /chat| API["FastAPI"]
    API -->|一般政策问法| F["匹配公开 FAQ · 返回标准答复与页码"]
    F --> M
    F --> UI
    API --> A["LangChain Agent · Qwen"]
    A -->|商品 / 订单 / 物流 / 工单| T["绑定 user_id 的业务工具"]
    T --> S[("SQLite 业务库")]
    A -->|店铺规则| K["search_knowledge"]
    PDF["顾客可见的 PDF"] --> P["标题层级切分 · 表格保留 · 页码溯源"]
    P --> E["BGE-M3 嵌入"]
    E --> M[("Milvus")]
    K --> M
    A -->|申请售后| R["确定性预审"]
    R --> S
    R --> D["可申请时生成草稿"]
    T --> O["工具结果与元数据"]
    K --> O
    D --> O
    O --> V["自然语言回答 + 商品卡 / 引用 / 调用记录 / 待确认动作"]
    V --> UI
```

政策查询先匹配公开 FAQ，手册查询通过 Dense + BM25 召回并由 RRF 合并。业务工具从当前演示用户上下文读取商品、订单和物流。

## 售后何时产生写操作

```mermaid
sequenceDiagram
    actor U as 顾客
    participant UI as 聊天页面
    participant A as Agent / apply_after_sales
    participant R as 售后规则服务
    participant DB as SQLite
    U->>UI: 提交订单号，申请退货
    UI->>A: POST /chat
    A->>R: 以当前用户身份预审
    R->>DB: 读取订单、商品状态及签收时间
    R-->>A: 可申请 / 不符合 / 需人工审核
    opt 返回草稿
        A-->>UI: action_required 和 request_id
        UI-->>U: 展示确认窗口
        U->>UI: 确认或取消
        UI->>R: POST /after-sales/confirm
        alt 确认且复核通过
            R->>DB: 按 request_id 幂等创建工单
            R-->>UI: 返回工单号
        else 取消
            R-->>UI: 返回取消结果
        end
    end
```

聊天工具直接调用预审服务。独立的 `/after-sales/preview` 入口通过 LangGraph 的 preview 节点调用同一服务。待确认草稿保存在进程内，服务重启后尚未提交的申请需要重新预审。已写入的工单可以按用户和请求号从业务库查回。

[聊天编排](../backend/agent/chat.py) · [工具定义](../backend/agent/tools.py) · [售后流程](../backend/agent/after_sales_graph.py) · [规则与写入](../backend/services/after_sales.py)
