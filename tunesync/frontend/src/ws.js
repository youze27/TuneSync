/**
 * WebSocket 进度管理器。
 *
 * - 连接 ws://<当前域名>/ws/progress
 * - 断线后指数退避重连：1s → 2s → 4s → 8s → 封顶 15s
 * - 消息通过回调分发给订阅者；连接状态写回 store 供顶栏展示
 */
import { reactive } from 'vue'

export const wsState = reactive({
  status: 'idle', // idle | connecting | open | closed
  retry: 0, // 当前退避秒数
  lastError: '',
  hello: '',
})

let socket = null
let retryTimer = null
let attempt = 0
let manualClose = false
const listeners = new Set()

const BACKOFF = [1000, 2000, 4000, 8000, 15000]

function delayOf(n) {
  return BACKOFF[Math.min(n, BACKOFF.length - 1)]
}

function setStatus(s, err = '') {
  wsState.status = s
  wsState.lastError = err
  if (s === 'open') {
    wsState.retry = 0
    attempt = 0
  } else if (s === 'connecting' || s === 'closed') {
    wsState.retry = Math.round(delayOf(attempt) / 1000)
  }
}

function wsUrl() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${proto}//${location.host}/ws/progress`
}

/** 订阅后端推送，返回取消订阅函数。 */
export function onWsMessage(fn) {
  listeners.add(fn)
  return () => listeners.delete(fn)
}

function dispatch(msg) {
  for (const fn of listeners) {
    try {
      fn(msg)
    } catch (e) {
      console.error('[ws] 处理消息出错', e)
    }
  }
}

function scheduleReconnect() {
  if (manualClose) return
  const ms = delayOf(attempt)
  attempt += 1
  wsState.retry = Math.round(ms / 1000)
  setStatus('closed')
  clearTimeout(retryTimer)
  retryTimer = setTimeout(connect, ms)
}

/** 建立连接（幂等：已连接或正在连接时直接返回）。 */
export function connect() {
  if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) {
    return
  }
  manualClose = false
  clearTimeout(retryTimer)
  setStatus('connecting')
  try {
    socket = new WebSocket(wsUrl())
  } catch (e) {
    setStatus('closed', String(e && e.message))
    scheduleReconnect()
    return
  }

  socket.onopen = () => setStatus('open')
  socket.onmessage = (ev) => {
    let msg
    try {
      msg = JSON.parse(ev.data)
    } catch {
      return
    }
    if (msg && msg.type === 'hello') wsState.hello = msg.message || ''
    dispatch(msg)
  }
  socket.onerror = () => {
    // onerror 之后浏览器会紧随 onclose，这里只记录，真正重连交给 onclose
    setStatus('closed', '连接出错')
  }
  socket.onclose = (ev) => {
    socket = null
    if (manualClose) {
      setStatus('idle')
      return
    }
    scheduleReconnect()
  }
}

/** 手动断开（不再自动重连）。 */
export function disconnect() {
  manualClose = true
  clearTimeout(retryTimer)
  if (socket) {
    try {
      socket.close()
    } catch {
      /* ignore */
    }
    socket = null
  }
  setStatus('idle')
}

/** 手动重连：重置退避计数后立即连接。 */
export function reconnectNow() {
  manualClose = false
  attempt = 0
  if (socket) {
    try {
      socket.close()
    } catch {
      /* ignore */
    }
    socket = null
  }
  connect()
}