/**
 * @name TuneSync 直连音源
 * @description 透明实现，无混淆：网易云(wy) 走 GD 音乐台取链。仅使用 lx.request 发普通 HTTP 请求。
 * @version 1.0.1
 * @author tunesync
 * @homepage https://music-api.gdstudio.xyz
 */

const { EVENT_NAMES, request, send, on } = lx

const GD_API = 'https://music-api.gdstudio.xyz/api.php'
const UA =
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'

// GD 音乐台的 br 取值：999=flac，320=320k，0=128k
const WY_BR = { flac: '999', '320k': '320', '128k': '0' }

function qs(base, params) {
  const parts = []
  for (const k in params) {
    const v = params[k]
    if (v === undefined || v === null || v === '') continue
    parts.push(k + '=' + encodeURIComponent(String(v)))
  }
  return base + '?' + parts.join('&')
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

function httpGet(url) {
  return new Promise((resolve, reject) => {
    let settled = false
    const abort = request(
      url,
      { method: 'get', timeout: 15000, headers: { 'User-Agent': UA, Accept: '*/*' } },
      (err, resp, body) => {
        if (settled) return
        settled = true
        if (err) return reject(new Error(String((err && err.message) || err)))
        const code = resp && resp.statusCode
        if (!code || code >= 400) return reject(new Error('HTTP ' + code))
        resolve(body)
      },
    )
    setTimeout(() => {
      if (settled) return
      settled = true
      try { if (abort) abort() } catch (e) { /* ignore */ }
      reject(new Error('timeout'))
    }, 18000)
  })
}

function pickId(info) {
  const i = info || {}
  let v = i.songmid || i.id || i.songid || ''
  if (typeof v !== 'string') v = String(v || '')
  if (!v) throw new Error('musicInfo 缺少歌曲 id')
  return v
}

function pickUrl(body) {
  const url = body && body.url
  if (typeof url === 'string' && /^https?:/i.test(url)) return url
  throw new Error('未取到链接: ' + JSON.stringify(body || null).slice(0, 150))
}

async function wyUrl(info, quality) {
  const url = qs(GD_API, {
    types: 'url',
    source: 'netease',
    id: pickId(info),
    br: WY_BR[quality] || WY_BR['128k'],
  })
  // GD 音乐台偶发 503 / 空响应，重试三次
  let lastErr
  for (let i = 0; i < 3; i++) {
    try {
      return pickUrl(await httpGet(url))
    } catch (e) {
      lastErr = e
      await sleep(400 * (i + 1))
    }
  }
  throw lastErr
}

send(EVENT_NAMES.inited, {
  sources: {
    wy: { type: 'music', actions: ['musicUrl'], qualitys: ['128k', '320k', 'flac'] },
  },
})

on(EVENT_NAMES.request, async ({ source, action, info }) => {
  if (action !== 'musicUrl') throw new Error('unsupported action: ' + String(action))
  const musicInfo = (info && info.musicInfo) || {}
  const quality = (info && info.type) || '128k'
  if (source === 'wy') return await wyUrl(musicInfo, quality)
  throw new Error('unsupported platform: ' + String(source))
})
