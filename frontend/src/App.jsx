import React, { useState, useRef, useEffect, useCallback } from 'react'
import ReactMarkdown from 'react-markdown'
import { api } from './api'

// 演示用户列表
const DEMO_USERS = [
  { id: 'U001', name: '演示用户1' },
  { id: 'U002', name: '演示用户2' },
]

// 订单状态中文映射（贴近真实电商业务状态）
const ORDER_STATUS_MAP = {
  pending_payment: { label: '待付款', color: '#fa8c16' },
  paid: { label: '已付款/待发货', color: '#1890ff' },
  shipped: { label: '已发货', color: '#722ed1' },
  delivered: { label: '已签收', color: '#52c41a' },
  returning: { label: '退货中', color: '#fa541c' },
  exchanging: { label: '换货中', color: '#13c2c2' },
  returned: { label: '已退货', color: '#8c8c8c' },
  cancelled: { label: '已取消', color: '#8c8c8c' },
  refunded: { label: '已退款', color: '#f5222d' },
}

// 格式化日期：2026-10-27T10:30:00+08:00 → 10-27
function formatDate(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${m}-${day}`
}

export default function App() {
  const [userId, setUserId] = useState('U001')
  const [messages, setMessages] = useState([])
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const [pendingAfterSales, setPendingAfterSales] = useState(null)
  const [orders, setOrders] = useState([])
  const [ordersLoading, setOrdersLoading] = useState(false)
  const [orderFilter, setOrderFilter] = useState('all')
  const scrollRef = useRef(null)
  const inputRef = useRef(null)
  // 每个用户一个 thread_id，用于多轮对话记忆
  const threadId = `thread-${userId}`
  // 消息唯一 ID 计数器
  const msgIdRef = useRef(0)
  const nextMsgId = () => ++msgIdRef.current

  // 按状态筛选后的订单列表
  const filteredOrders = orderFilter === 'all'
    ? orders
    : orders.filter((o) => o.status === orderFilter)

  // 加载用户订单
  const loadOrders = useCallback(async (uid) => {
    setOrdersLoading(true)
    try {
      const res = await api.listOrders(uid)
      if (res.success && res.data) {
        setOrders(res.data)
      }
    } catch (e) {
      setOrders([])
    } finally {
      setOrdersLoading(false)
    }
  }, [])

  // 切换用户时：重置对话 + 重新加载订单
  const handleUserChange = (newId) => {
    setUserId(newId)
    setMessages([])
    setPendingAfterSales(null)
    setOrderFilter('all')
    msgIdRef.current = 0
    loadOrders(newId)
  }

  // 首次加载 U001 的订单
  useEffect(() => {
    loadOrders('U001')
  }, [loadOrders])

  // 自动滚动到底部
  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' })
  }, [messages])

  // 输入框自动聚焦
  useEffect(() => {
    inputRef.current?.focus()
  }, [])

  // ESC 关闭售后弹窗
  useEffect(() => {
    if (!pendingAfterSales) return
    const onKey = (e) => {
      if (e.key === 'Escape') handleConfirm(false)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [pendingAfterSales])

  // 发送消息
  const sendMessage = async (text) => {
    const msg = (text || input).trim()
    if (!msg || loading) return

    const userMsg = { id: nextMsgId(), role: 'user', content: msg }
    setMessages((prev) => [...prev, userMsg])
    setInput('')
    setLoading(true)

    try {
      const res = await api.chat(msg, userId, threadId)
      if (res.success && res.data) {
        setMessages((prev) => [
          ...prev,
          {
            id: nextMsgId(),
            role: 'assistant',
            content: res.data.answer,
            citations: res.data.citations || [],
            products: res.data.products || [],
            traces: res.data.tool_traces || [],
          },
        ])
        // 对话内售后：Agent 返回 action_required 时弹出确认框
        if (res.data.action_required && res.data.action_required.action_type === 'after_sales_confirm') {
          setPendingAfterSales(res.data.action_required)
        }
      } else {
        setMessages((prev) => [...prev, { id: nextMsgId(), role: 'assistant', content: res.error || '请求失败，请稍后重试。' }])
      }
    } catch (e) {
      setMessages((prev) => [...prev, { id: nextMsgId(), role: 'assistant', content: '网络错误，请稍后重试。' }])
    } finally {
      setLoading(false)
    }
  }

  // 点击订单：直接在对话框发起查询
  const handleOrderClick = (order) => {
    const text = `帮我查一下订单 ${order.order_id} 的信息`
    sendMessage(text)
  }

  // 确认售后申请
  const handleConfirm = async (confirm) => {
    if (!pendingAfterSales) return
    setLoading(true)
    try {
      const res = await api.afterSalesConfirm(userId, pendingAfterSales.request_id, confirm)
      if (res.success && res.data) {
        setMessages((prev) => [
          ...prev,
          { id: nextMsgId(), role: 'assistant', content: res.data.message || (confirm ? '✅ 申请已提交' : '已取消申请') },
        ])
        // 售后确认后刷新订单列表，让最新状态（退货中/换货中）立即显示
        loadOrders(userId)
      } else {
        setMessages((prev) => [
          ...prev,
          { id: nextMsgId(), role: 'assistant', content: res.error || '确认操作失败。' },
        ])
      }
    } catch (e) {
      setMessages((prev) => [
        ...prev,
        { id: nextMsgId(), role: 'assistant', content: '确认操作失败，请稍后重试。' },
      ])
    } finally {
      setPendingAfterSales(null)
      setLoading(false)
    }
  }

  return (
    <div className="app">
      {/* 左侧边栏：订单列表 */}
      <aside className="sidebar">
        <div className="sidebar-header">
          <div className="brand">
            <span className="brand-icon">🛒</span>
            <span className="brand-name">星栈数码</span>
          </div>
          <div className="user-switch">
            <span className="user-label">演示身份</span>
            <select value={userId} onChange={(e) => handleUserChange(e.target.value)}>
              {DEMO_USERS.map((u) => (
                <option key={u.id} value={u.id}>{u.id} · {u.name}</option>
              ))}
            </select>
          </div>
        </div>

        <div className="sidebar-section">
          <div className="section-title">
            <span>我的订单</span>
            <span className="order-count">{filteredOrders.length}</span>
          </div>
          <select
            className="order-filter"
            value={orderFilter}
            onChange={(e) => setOrderFilter(e.target.value)}
          >
            <option value="all">全部状态</option>
            <option value="pending_payment">待付款</option>
            <option value="paid">已付款/待发货</option>
            <option value="shipped">已发货</option>
            <option value="delivered">已签收</option>
            <option value="returning">退货中</option>
            <option value="exchanging">换货中</option>
            <option value="cancelled">已取消</option>
            <option value="refunded">已退款</option>
          </select>
          <div className="order-list">
            {ordersLoading ? (
              <div className="empty-tip">加载中...</div>
            ) : filteredOrders.length === 0 ? (
              <div className="empty-tip">暂无订单</div>
            ) : (
              filteredOrders.map((o) => {
                const st = ORDER_STATUS_MAP[o.status] || { label: o.status, color: '#999' }
                return (
                  <div key={o.order_id} className="order-item" onClick={() => handleOrderClick(o)}>
                    <div className="order-top">
                      <span className="order-id">{o.order_id}</span>
                      <span className="order-status" style={{ color: st.color }}>{st.label}</span>
                    </div>
                    <div className="order-product" title={o.product_names || ''}>
                      {o.product_names || '（无商品信息）'}
                    </div>
                    <div className="order-bottom">
                      <span className="order-amount">¥{Number(o.amount_cny).toFixed(2)}</span>
                      <span className="order-date">{formatDate(o.placed_at)} · {o.item_count}件</span>
                    </div>
                  </div>
                )
              })
            )}
          </div>
        </div>
      </aside>

      {/* 右侧主区域：聊天 */}
      <main className="main">
        <header className="chat-header">
          <div className="chat-title">
            <span className="avatar">🤖</span>
            <div>
              <div className="chat-name">智能客服小星</div>
              <div className="chat-sub">在线 · 为您服务</div>
            </div>
          </div>
          {messages.length > 0 && (
            <button className="clear-btn" onClick={() => { setMessages([]); setPendingAfterSales(null); msgIdRef.current = 0 }}>
              清空对话
            </button>
          )}
        </header>

        <div className="chat-container" ref={scrollRef}>
          {messages.length === 0 && (
            <div className="welcome">
              <div className="welcome-icon">👋</div>
              <h2>您好，我是星栈数码智能客服</h2>
              <p>可以问我：商品推荐、订单物流、退货政策等问题</p>
              <div className="quick-actions">
                <button onClick={() => sendMessage('300元以内的主动降噪耳机有哪些？')} disabled={loading}>🎧 推荐ANC耳机</button>
                <button onClick={() => sendMessage('签收后多久可以申请退货？')} disabled={loading}>📖 退货政策</button>
                <button onClick={() => sendMessage('机械键盘有热插拔的吗？')} disabled={loading}>⌨️ 机械键盘推荐</button>
              </div>
            </div>
          )}

          {messages.map((msg) => (
            <div key={msg.id} className={`message ${msg.role}`}>
              {msg.role === 'assistant' ? (
                <div className="msg-avatar">🤖</div>
              ) : (
                <div className="msg-avatar user-avatar">👤</div>
              )}
              <div className="bubble">
                {msg.role === 'assistant' ? (
                  <div className="md-content">
                    <ReactMarkdown>{msg.content}</ReactMarkdown>
                  </div>
                ) : (
                  <p>{msg.content}</p>
                )}

                {/* 商品推荐卡片 */}
                {msg.products && msg.products.length > 0 && (
                  <div className="product-cards">
                    {msg.products.map((p) => (
                      <div key={p.product_id} className="product-card">
                        <div className="product-head">
                          <span className="product-name">{p.name}</span>
                          <span className="product-price">¥{p.price_cny}</span>
                        </div>
                        <div className="product-tags">
                          {(p.features || '').split(/[,，]/).filter(Boolean).slice(0, 4).map((f, idx) => (
                            <span key={idx} className="tag">{f.trim()}</span>
                          ))}
                        </div>
                        <div className="product-foot">
                          <span className={`stock ${p.stock > 0 ? 'in-stock' : 'out-stock'}`}>
                            {p.stock > 0 ? `库存 ${p.stock}` : '缺货'}
                          </span>
                          <span className="warranty">保修{p.warranty_months}月</span>
                        </div>
                      </div>
                    ))}
                  </div>
                )}

                {/* 知识来源 */}
                {msg.citations && msg.citations.length > 0 && (
                  <div className="citations">
                    <span className="citations-label">📚 来源</span>
                    {msg.citations.slice(0, 3).map((c, idx) => (
                      <span key={idx} className="citation-tag">{c.title} · 第{c.page_number}页</span>
                    ))}
                  </div>
                )}

                {/* 工具调用痕迹（可折叠） */}
                {msg.traces && msg.traces.length > 0 && (
                  <details className="traces">
                    <summary>🔧 工具调用 ({msg.traces.length})</summary>
                    {msg.traces.map((t, idx) => (
                      <div key={idx} className="trace-item">
                        <code>{t.tool}</code>
                        <span>{JSON.stringify(t.args)}</span>
                      </div>
                    ))}
                  </details>
                )}
              </div>
            </div>
          ))}

          {loading && (
            <div className="message assistant">
              <div className="msg-avatar">🤖</div>
              <div className="bubble typing">
                <span></span><span></span><span></span>
              </div>
            </div>
          )}
        </div>

        <div className="input-area">
          <input
            ref={inputRef}
            type="text"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && sendMessage()}
            placeholder="输入您的问题，按回车发送..."
            disabled={loading}
          />
          <button onClick={() => sendMessage()} disabled={loading || !input.trim()}>
            发送
          </button>
        </div>
      </main>

      {/* 售后确认弹窗 */}
      {pendingAfterSales && (
        <div className="modal-mask" onClick={() => handleConfirm(false)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <div className="modal-icon">📋</div>
            <h3>确认售后申请</h3>
            <p>您即将提交{isExchangeIssue(pendingAfterSales) ? '换货' : '退货'}申请，申请号：<code>{pendingAfterSales.request_id.slice(-8)}</code></p>
            <p className="modal-tip">提交后将创建工单，订单状态将更新为「{isExchangeIssue(pendingAfterSales) ? '换货中' : '退货中'}」，由客服人工审核处理。</p>
            <div className="modal-actions">
              <button className="btn-cancel" onClick={() => handleConfirm(false)}>取消</button>
              <button className="btn-confirm" onClick={() => handleConfirm(true)}>确认提交</button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

// 判断售后类型：非 no_reason（质量/错发/缺件）走换货流程
function isExchangeIssue(action) {
  const issueType = action?.payload?.issue_type
  return issueType && issueType !== 'no_reason'
}
