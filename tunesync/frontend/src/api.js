import axios from 'axios'
import { ElMessage } from 'element-plus'

/**
 * 统一 axios 实例：与后端同源，baseURL 固定为 /api。
 * 后端错误一律是 {"detail": "中文错误信息"}，这里统一抽出 detail 抛给调用方。
 */
const http = axios.create({
  baseURL: '/api',
  timeout: 30000,
})

/** 把 axios 错误转成可读中文文本。 */
export function errText(e, fallback = '请求失败') {
  const d = e?.response?.data?.detail
  if (typeof d === 'string' && d) return d
  if (Array.isArray(d) && d.length) {
    // FastAPI 参数校验错误
    return d.map((x) => `${(x.loc || []).slice(1).join('.')}: ${x.msg}`).join('；')
  }
  if (e?.code === 'ECONNABORTED') return '请求超时，请检查后端是否在运行'
  if (e?.response) return `接口返回 ${e.response.status}`
  return e?.message || fallback
}

/** 统一提示错误，页面里就不用重复 try/catch 弹窗。 */
export function toastError(e, fallback = '请求失败') {
  const msg = errText(e, fallback)
  ElMessage.error(msg)
  return msg
}

http.interceptors.response.use(
  (r) => r.data,
  (e) => {
    // 会话过期/未登录时后端一律回 401，这里统一弹一次提示并把地址交给页面跳登录页。
    // 登录接口本身的 401 是「口令错」，交给 Login.vue 自己展示，不要在这里抢跳。
    if (e?.response?.status === 401 && !e.config?.url?.includes('/auth/')) {
      // contract.mjs 在 Node 里运行没有 window，直接跳过浏览器事件广播
      if (typeof window !== 'undefined') window.dispatchEvent(new CustomEvent('ts:unauthorized'))
    }
    return Promise.reject(e)
  },
)

// ───────────────────────────── 登录

/** 启动时问一次：要不要登录、是否已登录、是否需要初始化账号。 */
export const getAuthState = () => http.get('/auth/state')
/** 首次启用鉴权时创建管理员账号。 */
export const setupAccount = (body) => http.post('/auth/setup', body)
export const login = (body) => http.post('/auth/login', body)
export const logout = () => http.post('/auth/logout')
/** 改口令。成功后该用户所有会话失效，需要重新登录。 */
export const changePassword = (body) => http.post('/auth/password', body)

// ───────────────────────────── 场景 / 设置

export const getCategories = () => http.get('/categories')

export const getSettings = () => http.get('/settings')
/** 局部覆盖：body 只带 preferences/llm/naming/library/download/scraper 等实际改动的节。 */
export const putSettings = (body) => http.put('/settings', body)
/** LLM 连通性测试：模型请求可能很慢，超时放宽到 130s。 */
export const testLlm = (override) => http.post('/settings/test-llm', override || {}, { timeout: 130000 })
/** 导出全部计划任务 + 配置为 JSON 对象（推送口令与 LLM 密钥不含在内）。 */
export const exportConfig = () => http.get('/config/export')
/** 导入导出文件：body 就是导出的那份 JSON，返回新建/更新计数。 */
export const importConfig = (data) => http.post('/config/import', data)

// ───────────────────────────── 计划任务

export const getTasks = () => http.get('/tasks')
export const createTask = (body) => http.post('/tasks', body)
export const updateTask = (id, body) => http.put(`/tasks/${id}`, body)
export const deleteTask = (id) => http.delete(`/tasks/${id}`)
export const runTask = (id) => http.post(`/tasks/${id}/run`)

/**
 * 立刻推送：用默认偏好跑一次，不必等 cron。
 * 传 prefs 时用界面上的草稿试跑（不会写回服务端默认值），不传则用已保存的。
 */
export const fetchNow = (prefs) => http.post('/fetch-now', prefs || {})

// ───────────────────────────── 运行历史

export const getRuns = (limit = 50) => http.get('/runs', { params: { limit } })
export const getLatestRun = () => http.get('/runs/latest')
export const getRun = (id) => http.get(`/runs/${id}`)
// 推送队列：正在跑的那一个 + 排队的若干个。WS 断了也能兜底查。
export const getRunQueue = () => http.get('/runs/queue')
export const getRunLogs = (id) => http.get('/runs/logs', { params: { ids: String(id) } })

// ───────────────────────────── 音源

export const getSources = () => http.get('/sources')
export const createSource = (body) => http.post('/sources', body)
export const updateSource = (id, body) => http.put(`/sources/${id}`, body)
export const deleteSource = (id) => http.delete(`/sources/${id}`)
export const checkSource = (id) => http.post(`/sources/${id}/check`)
export const checkAllSources = () => http.post('/sources/check-all')
/** 落雪式导入：粘贴 LX 脚本文本或链接，自动识别名称/平台建源（沙箱预检）。 */
export const quickImportLx = (body) => http.post('/sources/quick-lx', body, { timeout: 60000 })
/** 批量落雪导入：scripts 是脚本文本或链接的数组，逐个沙箱预检建源。 */
export const quickImportLxBatch = (scripts, platform) =>
  http.post('/sources/quick-lx-batch', { scripts, platform: platform || '' }, { timeout: 120000 })
export const getSourceHealth = () => http.get('/health/sources')

// ───────────────────────────── 推送平台

export const getTargets = () => http.get('/targets')
export const putTargets = (body) => http.put('/targets', body)
/**
 * 测试连接。draft 是界面上的当前输入——带上去才能测「你填的地址」，
 * 否则测的是已保存的旧值，改完没保存就点测试，报错会和屏幕上的对不上。
 * 密码栏的 *** 会被后端还原成已存的真值。
 */
export const testTarget = (kind, draft) => http.post(`/targets/${kind}/test`, draft || {})
export const getFeiniuSchema = () => http.get('/targets/feiniu/schema')
/** 飞牛歌单封面修复：把借用歌曲/专辑的封面复制到 playlist 封面目录并修复引用。 */
export const fixFeiniuCovers = () => http.post('/targets/feiniu/fix-covers')

// ───────────────────────────── 本地库 / 总览

export const rescanLibrary = () => http.post('/library/rescan')
export const getLibrary = () => http.get('/library')
export const getStatus = () => http.get('/status')

// 本地乐库：关键词 / 歌手 / 流派 / 风格 / 年份 / 格式 筛选
export const getLocalFiles = (params) => http.get('/localfiles', { params })
/** 下拉选项（歌手/流派/风格/格式/年份）。refresh=true 强制绕过缓存重建。 */
export const getLocalFacets = (refresh = false) => http.get('/localfiles/facets', { params: { refresh } })
/** 用选中的本地文件建歌单并写入已启用的推送平台（飞牛入库可能要等，超时放宽）。 */
export const createLocalPlaylist = (body) => http.post('/localfiles/playlist', body, { timeout: 180000 })

// 本地去重：先预览再删，删除要传 dry_run=false。async_run=true 走后台线程，
// 进度用 getDedupeStatus 轮询，避免 NAS 上删几千个文件把 HTTP 拖到超时。
export const getDuplicates = (scope = 'exact') => http.get('/library/duplicates', { params: { scope }, timeout: 120000 })
export const runDedupe = (body) => http.post('/library/dedupe', body, { timeout: 600000 })
export const getDedupeStatus = () => http.get('/library/dedupe/status')

// 手动刮削：scope=missing 只补没刮过的，all 全库重来，paths 手动指定
export const getScrapeStats = () => http.get('/scrape/stats')
export const getScrapeCandidates = (params) => http.get('/scrape/candidates', { params })
export const runScrape = (body) => http.post('/scrape/run', body)
export const getScrapeStatus = () => http.get('/scrape/status')
export const runScrapeAi = (body) => http.post('/scrape/ai-run', body)
export const getScrapeAiStatus = () => http.get('/scrape/ai/status')

// ───────────────────────────── 歌单导入

export const getPlaylistPlatforms = () => http.get('/playlist/platforms')
export const getPlaylistCategories = (platform) => http.get('/playlist/categories', { params: { platform } })
export const getPlaylistSquare = (platform, cat, page, limit = 30) =>
  http.get('/playlist/square', { params: { platform, cat, page, limit }, timeout: 45000 })
export const getPlaylistDetail = (platform, link) =>
  http.post('/playlist/detail', { platform, link }, { timeout: 90000 })
export const importPlaylist = (platform, link) => http.post('/playlist/import', { platform, link })
export const getImportRunning = (platform, pid) =>
  http.get('/playlist/import/running', { params: { platform, pid } })

// ───────────────────────────── 歌单管理

/** 哪些推送目标可以读/管歌单（飞牛 / Navidrome）。 */
export const getPlaylistTargets = () => http.get('/playlists/targets')
/** 列出目标端已有歌单。days>0 只返回生成时间早于该天数的老歌单。 */
export const getTargetPlaylists = (target, days = 0) =>
  http.get(`/playlists/${target}`, { params: { days }, timeout: 60000 })
/** 删除一张歌单（飞牛是删 playlist_track + playlist 两张表）。 */
export const deleteTargetPlaylist = (target, id) =>
  http.delete(`/playlists/${target}/${encodeURIComponent(id)}`, { timeout: 60000 })
/** 按生成时间清理老旧歌单。dry_run=true 只预览将被删的清单。 */
export const cleanupPlaylists = (body) =>
  http.post('/playlists/cleanup', body, { timeout: 180000 })