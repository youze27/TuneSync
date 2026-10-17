/**
 * TuneSync 示例 lx-music 音源脚本 —— 走 GD 音乐台公共 API（网易云曲库）。
 *
 * 用途：
 *   1. 验证 TuneSync 的 lx 沙箱 runner 全链路（on(request) / lx.request / send(inited)）；
 *   2. 当作可直接使用的音源：搜索走任意 http 模板，取链接交给本脚本。
 * 社区脚本（加密混淆的六源聚合脚本等）按同样方式导入即可，无需改动 runner。
 *
 * @name TuneSync 示例音源
 * @description GD 音乐台（网易云曲库）取链接
 * @version 1.0.0
 * @author TuneSync
 * @homepage https://music-api.gdstudio.xyz
 */
'use strict'

const API = 'https://music-api.gdstudio.xyz/api.php'
// lx 音质名 → GD br 参数（999 = flac/最高）
const BR = { 'flac24bit': '999', 'flac': '999', '320k': '320', '128k': '128' }

function get(url) {
  return new Promise((resolve, reject) => {
    lx.request(url, { method: 'get', timeout: 15000 }, (err, resp, body) => {
      if (err) return reject(err)
      resolve(body)
    })
  })
}

lx.on(lx.EVENT_NAMES.request, async ({ source, action, info }) => {
  if (action !== 'musicUrl') throw new Error('不支持的事件: ' + action)
  // musicInfo 来自 TuneSync 的搜索结果原始条目（raw）；探活等没有 raw 的场景回落到固定曲目
  const raw = (info && info.musicInfo) || {}
  const id = raw.id || (raw.raw && raw.raw.id) || '2652820720'
  const br = BR[info && info.type] || '999'
  const res = await get(`${API}?types=url&source=netease&id=${encodeURIComponent(id)}&br=${br}`)
  const url = res && res.url
  if (!url) throw new Error('GD 音乐台未返回链接（可能被限流或无版权）')
  return url
})

lx.send(lx.EVENT_NAMES.inited, {
  sources: {
    wy: { type: 'music', actions: ['musicUrl'], qualitys: ['128k', '320k', 'flac'] },
  },
})
