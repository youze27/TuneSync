/** 通用格式化与健康状态映射工具。 */

export const TRACK_STATUS = {
  local: { label: '本地复用', type: 'success' },
  done: { label: '已下载', type: 'primary' },
  downloaded: { label: '已下载', type: 'primary' },
  skipped: { label: '已跳过', type: 'info' },
  pending: { label: '等待中', type: 'info' },
  downloading: { label: '下载中', type: 'primary' },
  scraping: { label: '刮削中', type: 'primary' },
  error: { label: '失败', type: 'danger' },
  failed: { label: '失败', type: 'danger' },
}

export function trackStatusTag(s) {
  return TRACK_STATUS[s] || { label: s || '未知', type: 'info' }
}

export const HEALTH = {
  healthy: { label: '正常', type: 'success' },
  degraded: { label: '降级', type: 'warning' },
  down: { label: '熔断', type: 'danger' },
}

export function healthTag(h) {
  return HEALTH[h] || { label: h || '未知', type: 'info' }
}

export const RUN_STATUS = {
  running: { label: '运行中', type: 'primary' },
  success: { label: '成功', type: 'success' },
  partial: { label: '成功', type: 'success' },
  failed: { label: '失败', type: 'danger' },
}

export function runStatusTag(s) {
  return RUN_STATUS[s] || { label: s || '未知', type: 'info' }
}

export const PLAYLIST_STATUS = {
  pending: { label: '待处理', type: 'info' },
  running: { label: '处理中', type: 'primary' },
  done: { label: '完成', type: 'success' },
  success: { label: '完成', type: 'success' },
  error: { label: '失败', type: 'danger' },
  failed: { label: '失败', type: 'danger' },
  skipped: { label: '跳过', type: 'info' },
}

export function playlistStatusTag(s) {
  return PLAYLIST_STATUS[s] || { label: s || '未知', type: 'info' }
}

/** 2026-01-01 12:00:00 / ISO 串 → 1 月 2 日 12:00 */
export function fmtTime(s) {
  if (!s) return '—'
  const d = new Date(String(s).replace(' ', 'T'))
  if (Number.isNaN(d.getTime())) return String(s)
  const p = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

/** 相对时间：刚刚 / 3 分钟前 / 2 小时前 */
export function fmtAgo(s) {
  if (!s) return '从未'
  const d = new Date(String(s).replace(' ', 'T'))
  if (Number.isNaN(d.getTime())) return String(s)
  const sec = Math.floor((Date.now() - d.getTime()) / 1000)
  if (sec < 0) return fmtTime(s)
  if (sec < 60) return '刚刚'
  if (sec < 3600) return `${Math.floor(sec / 60)} 分钟前`
  if (sec < 86400) return `${Math.floor(sec / 3600)} 小时前`
  if (sec < 2592000) return `${Math.floor(sec / 86400)} 天前`
  return fmtTime(s)
}

export function fmtDuration(start, end) {
  if (!start) return '—'
  const a = new Date(String(start).replace(' ', 'T')).getTime()
  const b = end ? new Date(String(end).replace(' ', 'T')).getTime() : Date.now()
  if (Number.isNaN(a)) return '—'
  let sec = Math.max(0, Math.round((b - a) / 1000))
  const h = Math.floor(sec / 3600)
  const m = Math.floor((sec % 3600) / 60)
  const s = sec % 60
  if (h) return `${h} 小时 ${m} 分`
  if (m) return `${m} 分 ${s} 秒`
  return `${s} 秒`
}

/** Run.summary 可能是正常汇总，也可能是 {fatal: "..."}。 */
export function summaryOf(run) {
  const s = run?.summary
  if (!s || typeof s !== 'object') return { playlists: 0, local: 0, downloaded: 0, skipped: 0, pushed: 0, total: 0, errors: [], fatal: '' }
  const local = s.local ?? 0
  const downloaded = s.downloaded ?? 0
  const skipped = s.skipped ?? 0
  // 推送成功 = 本地复用 + 新下载；歌单总歌曲 = 上面三项之和（跳过的没进歌单）
  const pushed = local + downloaded
  return {
    playlists: s.playlists ?? 0,
    local,
    downloaded,
    skipped,
    pushed,
    total: pushed + skipped,
    errors: Array.isArray(s.errors) ? s.errors : [],
    fatal: s.fatal || '',
  }
}