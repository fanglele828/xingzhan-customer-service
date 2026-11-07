// API 封装：所有请求统一走 /api 前缀，由 Vite 代理到后端
const BASE = '/api'

async function post(path, body) {
  const res = await fetch(`${BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  return res.json()
}

async function get(path) {
  const res = await fetch(`${BASE}${path}`)
  return res.json()
}

export const api = {
  health: () => get('/health'),
  chat: (message, userId, threadId) => post('/chat', { message, user_id: userId, thread_id: threadId }),
  afterSalesPreview: (userId, orderId, issueType = 'no_reason', reason = '') =>
    post('/after-sales/preview', { user_id: userId, order_id: orderId, issue_type: issueType, reason }),
  afterSalesConfirm: (userId, requestId, confirm) =>
    post('/after-sales/confirm', { user_id: userId, request_id: requestId, confirm }),
  getTicket: (ticketId, userId) => get(`/tickets/${ticketId}?user_id=${userId}`),
  listOrders: (userId, status) => get(`/orders?user_id=${userId}${status ? '&status=' + status : ''}`),
}
