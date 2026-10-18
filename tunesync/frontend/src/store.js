/**
 * 轻量全局响应式状态（不引入 Pinia）。
 * 负责：场景字典、仪表盘状态、WebSocket 实时进度与日志流。
 */
import { computed, reactive } from 'vue'
import * as api from './api.js'
import { onWsMessage, wsState } from './ws.js'

// 一次推送会为每首歌推 1~2 条消息（track + scrape），一条 run 动辄几百上千条。
// 这里的两个上限只是防跑飞内存的兜底，正常情况下等同「全量保留」。
export const MAX_TRACKS = 5000
export const MAX_PROGRESS = 8000

/** 分类分组展示顺序与中文名。 */
export const GROUP_LABELS = {
  special: '置顶',
  activity: '按活动',
  mood: '按心情',
  style: '按风格',
}
export const GROUP_ORDER = ['special', 'activity', 'mood', 'style']

/** 歌单来源两档。key 是界面上的选项，后端仍落到 pipeline.PUSH_MODES 的三档。 */
export const SOURCE_MODES = [
  { key: 'ai', label: 'AI 生成', desc: '歌单与曲目全部由模型给出，不使用任何平台歌单' },
  { key: 'source', label: '乐源拉取', desc: '从音源平台拉真实歌单，可再让模型筛一轮' },
]
export const SOURCE_MODE_KEYS = SOURCE_MODES.map((m) => m.key)

/** 场景怎么推：单独推送 = 每个场景各跑一轮；合并推送 = 合成一个主题只跑一轮。 */
export const SCENE_MODES = [
  { key: 'separate', label: '单独推送', desc: '每个场景各推一轮，歌单归属各自归位' },
  { key: 'merge', label: '合并推送', desc: '所有场景合成一个综合主题，只推一轮' },
]

/** 每张歌单的曲目数可选值（0 = 沿用全局设置里的全局值）。 */
export const TRACKS_PER_OPTIONS = [0, 10, 15, 20, 30, 40, 50, 80, 100]

/**
 * 界面上的「歌单来源 + AI 筛选」合成后端 push_mode。
 * AI 生成 → llm；乐源拉取 + 筛选 → source_llm；乐源拉取不过滤 → source_raw。
 */
export function pushModeOf(source, aiFilter) {
  if (source !== 'source') return 'llm'
  return aiFilter ? 'source_llm' : 'source_raw'
}

/** 反向拆解：push_mode → { source, aiFilter }。 */
export function splitPushMode(mode) {
  if (mode === 'source_llm') return { source: 'source', aiFilter: true }
  if (mode === 'source_raw') return { source: 'source', aiFilter: false }
  return { source: 'ai', aiFilter: true }
}

/** 只有乐源拉取才需要选来源平台。 */
export const needsSourcePlatforms = (mode) => mode === 'source_llm' || mode === 'source_raw'

/** 歌单来源的展示名，供列表页用。 */
export function sourceModeLabel(mode) {
  const { source, aiFilter } = splitPushMode(mode)
  return source === 'source' ? (aiFilter ? '乐源拉取 + AI 筛选' : '乐源拉取') : 'AI 生成'
}

/** 后端仍然用三档 push_mode，这里只做展示名，供运行历史等旧位置继续用。 */
export const pushModeLabel = (key) => sourceModeLabel(key)

/** 乐源拉取的来源平台；['all'] 表示多平台聚合。 */
export const SOURCE_PLATFORMS = [
  { key: 'all', name: '全平台聚合' },
  { key: 'wy', name: '网易云' },
  { key: 'tx', name: 'QQ 音乐' },
  { key: 'kg', name: '酷狗' },
  { key: 'kw', name: '酷我' },
  { key: 'mg', name: '咪咕' },
]

/**
 * 归一来源平台选择：非法项丢弃，全选/空选等价于聚合。
 * 与后端 pipeline.normalise_source_platforms 保持同一条规则，
 * 免得界面上存进去的值被服务端改写成另一份。
 */
export function normaliseSourcePlatforms(list) {
  const valid = (list || []).filter((p) => SOURCE_PLATFORMS.some((s) => s.key === p))
  return valid.length ? valid : ['all']
}

export const state = reactive({
  booted: false,
  loading: false,
  error: '',

  categories: [],
  catMap: {},

  status: null, // GET /api/status
  library: { files: 0, scanned_at: null },
  lastRunDetail: null, // GET /api/runs/latest（含 playlists）
  tasks: [],
  settings: null, // GET /api/settings（含 preferences / cron 约定 / defaults）

  // 实时进度。推送已改成全局串行队列，但界面仍可能同时存在多个任务
  // （一个在跑、几个在排队、以及刚结束还没被新任务覆盖的），
  // 所以进度按 run_id 分片存，runs[id] 各自独立，谁也不覆盖谁。
  runs: {}, // { [runId]: { runId, taskId, taskName, active, startedAt, progress, tracks, totals, ended } }
  liveId: null, // 当前聚焦展示的那一片
  queue: { active: null, waiting: [], depth: 0 }, // GET /api/runs/queue
})

/** 新建（或取回）某个 run 的进度分片。 */
export function runSlot(runId) {
  if (!runId) return null
  if (!state.runs[runId]) {
    state.runs[runId] = {
      runId,
      taskId: null,
      taskName: '',
      active: false,
      startedAt: '',
      progress: [],
      tracks: [],
      totals: { local: 0, downloaded: 0, skipped: 0 },
      ended: null,
      // 进度条口径：expected 由后端 run_expected 事件累加（每张歌单开跑时
      // 报上它的曲目数），processed = totals 三项之和。两栏都有才算可信百分比。
      expected: 0,
      logsLoaded: false,
    }
  }
  return state.runs[runId]
}

/** 聚焦的进度分片；还没有任何运行时返回 null，模板里用 EMPTY_LIVE 兜底。 */
export const liveRun = computed(() => state.runs[state.liveId] || null)

/** 有任务正在跑（不含排队）。顶部的「运行中」标记看它。 */
export const anyRunning = computed(() =>
  Object.values(state.runs).some((r) => r.active))

// 页面刷新后 runs 是空的，从后端补一次最近的几条，
// 不然 WS 还没推第一个事件时面板会一直空白。
export async function loadRuns() {
  try {
    const rows = await api.getRuns(20)
    rows.forEach((r) => {
      const slot = runSlot(r.id)
      slot.taskId = r.task_id
      slot.taskName = r.task_name || ''
      slot.active = r.status === 'running'
      slot.startedAt = r.started_at || ''
      slot.ended = r.summary ? JSON.parse(r.summary) : null
    })
    if (!state.liveId && rows.length) state.liveId = rows[0].id
    // 还在跑的运行补拉留档日志：WS 只推给当时在线的页面，
    // 半路打开/刷新后阶段日志和歌曲进度都靠这个补齐
    await Promise.all(rows
      .filter((r) => r.status === 'running')
      .map((r) => hydrateRunLogs(state.runs[r.id])))
  } catch {
    /* 拉不到就等 WS 推 */
  }
}

/**
 * 从 GET /runs/logs 补齐某个 run 的留档日志。
 * 只在本地一片空白时拉（避免和 WS 推的增量重复）；
 * 拉完 logsLoaded 置位，之后的进度都走 WS。
 */
async function hydrateRunLogs(slot) {
  if (!slot || slot.logsLoaded || slot.progress.length || slot.tracks.length) return
  slot.logsLoaded = true
  try {
    const data = await api.getRunLogs(slot.runId)
    const s = data?.[String(slot.runId)] || data?.[slot.runId]
    if (!s) return
    ;(s.progress || []).forEach((p) => pushProgress(slot, { stage: p.stage, detail: p.detail }))
    ;(s.tracks || []).forEach((t) => {
      if (t.status && slot.totals[t.status] !== undefined) slot.totals[t.status] += 1
      slot.tracks.push({
        title: t.title, artist: t.artist, status: t.status,
        source: t.source, file_path: t.file_path, error: t.error, at: '',
      })
    })
    if (slot.tracks.length > MAX_TRACKS) {
      slot.tracks.splice(0, slot.tracks.length - MAX_TRACKS)
    }
  } catch {
    /* 拉不到就等 WS 推 */
  }
}

export async function loadQueue() {
  try {
    state.queue = await api.getRunQueue()
  } catch {
    /* 拉不到就等 WS 推 */
  }
}

export const ws = wsState

// ───────────────────────────── 登录态

/**
 * 鉴权状态。enabled=false 时后端完全放行，前端也别挡路。
 * needSetup 表示启用了鉴权但一个账号都没有，得先初始化管理员。
 */
export const auth = reactive({
  enabled: false,
  authenticated: false,
  user: null,
  needSetup: false,
  ssoReady: false,
  checked: false,
})

let authPromise = null

/**
 * 拉一次鉴权状态。路由守卫与 boot() 可能同时触发，共享同一个 in-flight 请求；
 * 失败后清空缓存，允许下次重试。登录/登出后要重新问后端，用 loadAuth(true) 强制刷新。
 */
export function loadAuth(force = false) {
  if (force || !authPromise) {
    authPromise = api
      .getAuthState()
      .then((s) => {
        const v = s || {}
        auth.enabled = !!v.enabled
        auth.authenticated = !!v.authenticated
        auth.user = v.user || null
        auth.needSetup = !!v.need_setup
        auth.ssoReady = !!v.sso_ready
        auth.checked = true
        return auth
      })
      .catch((e) => {
        authPromise = null
        throw e
      })
  }
  return authPromise
}

/** 会话在页面上被清掉（如 401），重置缓存让下一次守卫重新问后端。 */
export function markUnauthenticated() {
  auth.authenticated = false
  auth.user = null
  authPromise = null
}

export const catByCode = computed(() => state.catMap)

/** 分组后的分类，special 组（「最近更新」）置顶。 */
export const groupedCategories = computed(() => {
  const out = {}
  for (const g of GROUP_ORDER) out[g] = []
  for (const c of state.categories) {
    const g = c.group || 'style'
    if (!out[g]) out[g] = []
    out[g].push(c)
  }
  return out
})

export const catName = (code) => state.catMap[code]?.name || code

// ───────────────────────────── 全局设置（preferences / cron 约定）

/** preferences 的缺省值；后端没配 preferences 节时用它兜底。 */
export const PREF_FALLBACK = {
  categories: [],
  count_per: 3,
  custom_prompt: '',
  push_mode: 'llm',
  source_platforms: ['all'],
}

/** 服务端返回的 preferences（已合并 config.yaml 默认值与 UI 覆盖）。 */
export const preferences = computed(() => ({
  ...PREF_FALLBACK,
  ...(state.settings?.preferences || {}),
}))

/** 用户通过 UI 覆盖过的部分（GET /api/settings 的 defaults），用于提示「已自定义」。 */
export const overriddenPrefs = computed(() => state.settings?.defaults?.preferences || {})

/** 后端公布的 cron 编号约定（fields / day_of_week / example），为空表示尚未加载。 */
export const cronHelp = computed(() => state.settings?.cron || null)

/**
 * 从 cron.day_of_week 文案里解析出「编号 → 星期名」。
 *
 * 后端文案形如 "0=周一 1=周二 … 6=周日（7 也按周日处理）"，
 * 这样星期约定只在后端定义一处，前端不再硬编码第二份。
 */
export function parseWeekdayMap(help) {
  const out = {}
  for (const m of String(help?.day_of_week || '').matchAll(/(\d+)\s*=\s*([^\s（(]+)/g)) {
    out[Number(m[1])] = m[2]
  }
  return out
}

/** settings 未加载成功时的退化星期名，仅用于显示，不会写进 cron 表达式。 */
const WEEKDAY_FALLBACK = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']

/** 下标 0–6 对应的星期名，优先采用后端公布的约定。 */
export const weekdayLabels = computed(() => {
  const map = parseWeekdayMap(state.settings?.cron)
  return WEEKDAY_FALLBACK.map((fallback, i) => map[i] || fallback)
})

// ───────────────────────────── 加载

export async function loadDictionary() {
  const cats = await api.getCategories()
  state.categories = Array.isArray(cats) ? cats : []
  state.catMap = Object.fromEntries(state.categories.map((c) => [c.code, c]))
}

export async function loadStatus() {
  state.status = await api.getStatus()
}

export async function loadLibrary() {
  state.library = (await api.getLibrary()) || { files: 0, scanned_at: null }
}

let settingsPromise = null

/**
 * 取一次全局设置并缓存，供 CronPicker、偏好页、新建任务默认值共用。
 * 并发调用共享同一个 in-flight 请求；失败后清空缓存，允许下次重试。
 */
export function ensureSettings(force = false) {
  if (force || !settingsPromise) {
    settingsPromise = api
      .getSettings()
      .then((s) => {
        state.settings = s || {}
        return state.settings
      })
      .catch((e) => {
        settingsPromise = null
        throw e
      })
  }
  return settingsPromise
}

export async function loadTasks() {
  state.tasks = (await api.getTasks()) || []
  return state.tasks
}

export async function loadLatestRun() {
  state.lastRunDetail = (await api.getLatestRun()) || null
  return state.lastRunDetail
}

export async function refreshDashboard() {
  state.loading = true
  try {
    // loadRuns/loadQueue 内部各自 try/catch，WS 还没推第一个事件时靠它们兜底
    await Promise.all([
      loadStatus(), loadLibrary(), loadLatestRun(), loadTasks(), loadRuns(), loadQueue(),
    ])
    state.error = ''
  } catch (e) {
    state.error = api.errText(e, '加载仪表盘数据失败')
  } finally {
    state.loading = false
  }
}

export async function boot() {
  // 先问鉴权：没登录时后面所有接口都会 401，白拉一次不如直接去登录页。
  try {
    await loadAuth()
  } catch (e) {
    state.error = api.errText(e, '读取登录状态失败')
  }
  if (auth.enabled && !auth.authenticated) {
    state.booted = true
    return
  }
  try {
    await loadDictionary()
  } catch (e) {
    state.error = api.errText(e, '加载场景失败')
  }
  // cron 星期约定与新建任务默认值都来自 settings；失败不阻塞页面渲染。
  ensureSettings().catch(() => {})
  state.booted = true
}

// ───────────────────────────── WebSocket 事件处理

const STAGE_LABELS = {
  fetch: '生成歌单',
  'fetch-error': '生成失败',
  scrape: '刮削',
  scan: '扫描本地库',
  write: '写入歌单',
  library: '本地库',
}

export function stageLabel(stage) {
  return STAGE_LABELS[stage] || stage || '进度'
}

function pushProgress(slot, item) {
  slot.progress.push({ ...item, at: new Date().toLocaleTimeString('zh-CN', { hour12: false }) })
  if (slot.progress.length > MAX_PROGRESS) {
    slot.progress.splice(0, slot.progress.length - MAX_PROGRESS)
  }
}

/** 处理一条后端推送，写入对应 run 的进度分片。 */
export function handleWsMessage(msg) {
  if (!msg || !msg.type) return
  switch (msg.type) {
    case 'hello':
      break

    case 'resync':
      // WS 重连：断线期间后端的进度/结束事件全丢了，用 REST 拉回权威状态
      loadRuns()
      loadQueue()
      break

    case 'queue':
      // 队列变化：正在跑谁、后面排了几个。不带 run_id，靠 runId 关联不到分片，
      // 只更新队列本身，进度面板另外按 run_start/run_end 走。
      state.queue = {
        active: msg.active || null,
        waiting: msg.waiting || [],
        depth: msg.depth || 0,
      }
      break

    case 'run_start': {
      // 新开一片而不是覆盖旧的：队列里前一个任务结束时可能还没被看到
      const slot = runSlot(msg.run_id)
      slot.active = true
      slot.taskId = msg.task_id
      slot.taskName = typeof msg.task === 'string' ? msg.task : msg.task?.name || ''
      slot.startedAt = new Date().toLocaleString('zh-CN', { hour12: false })
      slot.progress = []
      slot.tracks = []
      slot.totals = { local: 0, downloaded: 0, skipped: 0 }
      slot.ended = null
      slot.expected = 0
      slot.logsLoaded = true
      // 只有当前没有正在展示的运行（或者展示的那片已经结束）才自动切前台，
      // 否则排队里第二个任务开始时会把第一个还在跑的进度抢走。
      const cur = state.liveId ? state.runs[state.liveId] : null
      if (!cur || cur === slot || !cur.active) state.liveId = msg.run_id
      pushProgress(slot, { stage: 'run', detail: `任务「${slot.taskName || msg.run_id}」开始运行` })
      break
    }

    case 'run_expected': {
      // 每张歌单开跑时后端报上曲目数，进度条分母累加——
      // 总量随场景拉取逐步变大，百分比会小幅回跳，这是诚实进度
      const slot = msg.run_id ? runSlot(msg.run_id) : liveRun.value
      if (slot) slot.expected = (slot.expected || 0) + (msg.add || 0)
      break
    }

    case 'progress': {
      const slot = msg.run_id ? runSlot(msg.run_id) : liveRun.value
      if (!slot) break
      slot.active = true
      pushProgress(slot, { stage: msg.stage, detail: msg.detail, run_playlist_id: msg.run_playlist_id })
      break
    }

    case 'track': {
      const slot = msg.run_id ? runSlot(msg.run_id) : liveRun.value
      if (!slot) break
      if (msg.status && slot.totals[msg.status] !== undefined) {
        slot.totals[msg.status] += 1
      }
      slot.tracks.push({
        title: msg.title,
        artist: msg.artist,
        status: msg.status,
        source: msg.source,
        file_path: msg.file_path,
        error: msg.error,
        at: new Date().toLocaleTimeString('zh-CN', { hour12: false }),
      })
      if (slot.tracks.length > MAX_TRACKS) slot.tracks.shift()
      break
    }

    case 'run_end': {
      const slot = msg.run_id ? runSlot(msg.run_id) : liveRun.value
      if (!slot) break
      slot.active = false
      slot.ended = msg.summary || null
      pushProgress(slot, { stage: 'run-end', detail: '本次运行结束' })
      break
    }

    case 'source-health':
      // 音源健康变化时同步到 status.sources，便于各页面标签即时变色
      if (state.status?.sources) {
        const hit = state.status.sources.find(
          (s) => s.name === msg.source || String(s.id) === String(msg.source),
        )
        if (hit) hit.health = msg.health
      }
      break

    default:
      break
  }
}

let unsubscribe = null
export function bindWs() {
  if (unsubscribe) return unsubscribe
  unsubscribe = onWsMessage(handleWsMessage)
  return unsubscribe
}