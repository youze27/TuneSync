#!/usr/bin/env node
/**
 * LX-Music 音源脚本沙箱 runner —— TuneSync 与 lx 音源脚本之间的桥。
 *
 * 依据 lx-music-desktop userApi preload（复刻自 music-hub 项目的 lx-env/sandbox-worker，
 * 2026-10-12 移植）构造 globalThis.lx，在 node:vm 沙箱里执行第三方音源脚本。
 *
 * 与 lx-music-desktop preload.js 的对齐点：
 *   - EVENT_NAMES { request, inited, updateAlert }
 *   - send(eventName, data) → Promise；on(eventName, handler)
 *   - request(url, options, callback) → abort()（默认不跟随重定向，与桌面版一致）
 *   - utils.crypto { aesEncrypt, rsaEncrypt, randomBytes, md5 }
 *   - utils.buffer { from, bufToString }；utils.zlib { inflate, deflate }
 *   - currentScriptInfo / version '2.0.0' / env 'desktop'
 *
 * 行协议（stdin/stdout 每行一个 JSON；脚本 console 输出走 stderr，不污染协议）：
 *   → {"id":1,"cmd":"load","name":"...","script":"<raw js>"}
 *   ← {"id":1,"ok":true,"result":{"sources":{...}}}   脚本 send(inited) 后才返回，15s 超时
 *   → {"id":2,"cmd":"call","source":"wy","action":"musicUrl","info":{...}}
 *   ← {"id":2,"ok":true,"result":"https://..."}       30s 超时
 *   出错一律 {"id":N,"ok":false,"message":"..."}；进程级致命错误退出码 1。
 */
'use strict'
const vm = require('node:vm')
const http = require('node:http')
const https = require('node:https')
const zlib = require('node:zlib')
const crypto = require('node:crypto')
const readline = require('node:readline')

const EVENT_NAMES = { request: 'request', inited: 'inited', updateAlert: 'updateAlert' }

/** 与 lx-music-desktop 一致的平台能力矩阵（local 只在桌面版有意义，这里不放开） */
const ALL_SOURCES = ['kw', 'kg', 'tx', 'wy', 'mg']
const SUPPORT_QUALITYS = {
  kw: ['128k', '320k', 'flac', 'flac24bit'],
  kg: ['128k', '320k', 'flac', 'flac24bit'],
  tx: ['128k', '320k', 'flac', 'flac24bit'],
  wy: ['128k', '320k', 'flac', 'flac24bit'],
  mg: ['128k', '320k', 'flac', 'flac24bit'],
}
const SUPPORT_ACTIONS = { kw: ['musicUrl'], kg: ['musicUrl'], tx: ['musicUrl'], wy: ['musicUrl'], mg: ['musicUrl'] }

/** preload.js 同款 URL 校验 */
function verifyUrl(response) {
  if (typeof response !== 'string' || response.length > 2048 || !/^https?:/.test(response)) {
    throw new Error('脚本返回的不是合法 URL')
  }
  return response
}

let scriptInfo = { name: '', description: '', version: '', author: '', homepage: '', rawScript: '' }
let requestHandler = null
let isInited = false
let initedResolve = null
let initedReject = null
let initedSources = null
let updateAlertShown = false

/**
 * 从脚本头注释解析 @name/@description/@version/@author/@homepage。
 * lx-music 桌面版把这段注入 currentScriptInfo，很多社区脚本拿它做
 * 防篡改自校验（md5(name+description) 对不上就拒绝 inited 甚至死循环），
 * 所以必须解析头注释而不是用调用方传入的名称。
 */
function parseScriptMeta(script) {
  const head = String(script || '').slice(0, 8192)
  const meta = {}
  for (const key of ['name', 'description', 'version', 'author', 'homepage']) {
    const m = head.match(new RegExp('@' + key + '[ \\t]+([^\\n\\r]+)'))
    if (m) meta[key] = m[1].split('*/')[0].trim()
  }
  return meta
}

/** lx.request —— node:http 封装，行为对齐 preload.js 的 needle 版本 */
function lxRequest(url, options, callback) {
  const { method = 'get', timeout, headers = {}, body, form, formData } = options || {}
  try {
    const u = new URL(url)
    const mod = u.protocol === 'https:' ? https : http
    const hdrs = { ...headers }
    let data = null
    if (body !== undefined && body !== null) {
      data = typeof body === 'string' || Buffer.isBuffer(body) ? body : JSON.stringify(body)
      const ct = hdrs['Content-Type'] || hdrs['content-type']
      if (!ct && typeof body !== 'string' && !Buffer.isBuffer(body)) hdrs['Content-Type'] = 'application/json'
    } else if (form !== undefined && form !== null) {
      data = new URLSearchParams(form).toString()
      if (!hdrs['Content-Type'] && !hdrs['content-type']) hdrs['Content-Type'] = 'application/x-www-form-urlencoded'
    } else if (formData !== undefined && formData !== null) {
      // 桌面版走 multipart；社区脚本几乎只用 form/body，这里退化为 urlencoded
      data = new URLSearchParams(formData).toString()
      if (!hdrs['Content-Type'] && !hdrs['content-type']) hdrs['Content-Type'] = 'application/x-www-form-urlencoded'
    }
    const req = mod.request(
      {
        hostname: u.hostname,
        port: u.port || (u.protocol === 'https:' ? 443 : 80),
        path: u.pathname + u.search,
        method: String(method).toUpperCase(),
        headers: hdrs,
      },
      (res) => {
        const chunks = []
        res.on('data', (c) => chunks.push(c))
        res.on('end', () => {
          try {
            const raw = Buffer.concat(chunks)
            let respBody = raw.toString('utf8')
            try {
              respBody = JSON.parse(respBody)
            } catch { /* keep string */ }
            callback(null, {
              statusCode: res.statusCode,
              statusMessage: res.statusMessage,
              headers: res.headers,
              bytes: raw.length,
              raw,
              body: respBody,
            }, respBody)
          } catch (e) {
            callback(e, null, null)
          }
        })
      },
    )
    if (typeof timeout === 'number' && timeout > 0) req.setTimeout(Math.min(timeout, 60000))
    req.on('timeout', () => req.destroy(new Error('Request timeout')))
    // 脚本的回调抛异常只记录，绝不能变成 uncaughtException 把 runner 杀掉
    // （死域名的 error 回调、脚本自己的 bug 都走过这里）
    req.on('error', (err) => {
      try {
        callback(err, null, null)
      } catch (e) {
        process.stderr.write(`[lx:request-callback] ${e && e.stack || e}\n`)
      }
    })
    if (data !== null) req.write(data)
    req.end()
    return () => { req.destroy() }
  } catch (err) {
    try {
      callback(err, null, null)
    } catch (e) {
      process.stderr.write(`[lx:request-callback] ${e && e.stack || e}\n`)
    }
    return () => {}
  }
}

function buildGlobal() {
  return {
    EVENT_NAMES,
    request: lxRequest,
    send: (eventName, data) =>
      new Promise((resolve, reject) => {
        switch (eventName) {
          case EVENT_NAMES.inited: {
            if (isInited) return reject(new Error('Script is inited'))
            isInited = true
            const info = data || {}
            const sources = {}
            for (const s of ALL_SOURCES) {
              const us = info.sources && info.sources[s]
              if (!us || us.type !== 'music') continue
              sources[s] = {
                type: 'music',
                actions: (SUPPORT_ACTIONS[s] || []).filter((a) => (us.actions || []).includes(a)),
                qualitys: (SUPPORT_QUALITYS[s] || []).filter((q) => (us.qualitys || []).includes(q)),
              }
            }
            initedSources = sources
            if (initedResolve) { initedResolve(sources); initedResolve = null; initedReject = null }
            resolve()
            break
          }
          case EVENT_NAMES.updateAlert: {
            if (updateAlertShown) return reject(new Error('The update alert can only be called once.'))
            updateAlertShown = true
            resolve()
            break
          }
          default:
            reject(new Error('The event is not supported: ' + eventName))
        }
      }),
    on: (eventName, handler) =>
      new Promise((resolve, reject) => {
        if (eventName !== EVENT_NAMES.request) return reject(new Error('The event is not supported: ' + eventName))
        requestHandler = handler
        resolve()
      }),
    utils: {
      crypto: {
        aesEncrypt(buffer, mode, key, iv) {
          const cipher = crypto.createCipheriv(mode, key, iv)
          return Buffer.concat([cipher.update(buffer), cipher.final()])
        },
        rsaEncrypt(buffer, key) {
          buffer = Buffer.concat([Buffer.alloc(128 - buffer.length), buffer])
          return crypto.publicEncrypt({ key, padding: crypto.constants.RSA_NO_PADDING }, buffer)
        },
        randomBytes: (size) => crypto.randomBytes(size),
        md5: (str) => crypto.createHash('md5').update(str).digest('hex'),
      },
      buffer: {
        from: (...args) => Buffer.from(...args),
        bufToString: (buf, format) => Buffer.from(buf, 'binary').toString(format),
      },
      zlib: {
        inflate: (buf) => new Promise((resolve, reject) => {
          zlib.inflate(buf, (err, data) => (err ? reject(new Error(err.message)) : resolve(data)))
        }),
        deflate: (data) => new Promise((resolve, reject) => {
          zlib.deflate(data, (err, buf) => (err ? reject(new Error(err.message)) : resolve(buf)))
        }),
      },
    },
    currentScriptInfo: { ...scriptInfo },
    version: '2.0.0',
    env: 'desktop',
  }
}

/** 调用脚本注册的 request handler 并按 action 校验返回值 */
async function callAction(source, action, info) {
  if (!requestHandler) throw new Error('Request event is not defined')
  const response = await requestHandler({ source, action, info })
  if (action === 'musicUrl' || action === 'pic') return verifyUrl(response)
  throw new Error('Unknown action: ' + String(action))
}

/** 完整 console 代理：混淆脚本可能调用任意 console 方法，缺失会抛怪错 */
function makeConsole() {
  const proxy = {}
  for (const k of ['log', 'info', 'warn', 'error', 'debug', 'trace', 'dir', 'group', 'groupCollapsed',
    'groupEnd', 'table', 'time', 'timeEnd', 'timeLog', 'count', 'countReset', 'assert']) {
    proxy[k] = function (...args) {
      process.stderr.write(`[lx:${scriptInfo.name}] ${args.map(String).join(' ')}\n`)
    }
  }
  return proxy
}

function writeLine(msg) {
  process.stdout.write(JSON.stringify(msg) + '\n')
}

function runInSandbox(script) {
  const sandbox = {
    lx: buildGlobal(),
    console: makeConsole(),
    setTimeout,
    clearTimeout,
    setInterval,
    clearInterval,
    // 不暴露 require / process / module / Buffer —— 脚本只能用 lx.* 能力
  }
  sandbox.globalThis = sandbox
  sandbox.window = sandbox
  vm.createContext(sandbox, { name: `lx-source:${scriptInfo.name}` })
  vm.runInContext(script, sandbox, { filename: `${scriptInfo.name}.js`, timeout: 30000 })
}

const rl = readline.createInterface({ input: process.stdin, terminal: false })

rl.on('line', (line) => {
  let msg
  try {
    msg = JSON.parse(line)
  } catch {
    return
  }
  if (msg.cmd === 'load') {
    // currentScriptInfo 以脚本头注释为准（@name 等缺失时才回落到调用方传入的名称）
    const meta = parseScriptMeta(msg.script)
    scriptInfo = {
      name: meta.name || String(msg.name || 'lx-source'),
      description: meta.description || String(msg.description || ''),
      version: meta.version || String(msg.version || ''),
      author: meta.author || String(msg.author || ''),
      homepage: meta.homepage || String(msg.homepage || ''),
      rawScript: String(msg.script || ''),
    }
    isInited = false
    updateAlertShown = false
    requestHandler = null
    initedSources = null
    const timer = setTimeout(() => {
      if (initedReject) {
        initedReject(new Error('脚本 15s 内未调用 send(inited)'))
        initedResolve = null; initedReject = null
        writeLine({ id: msg.id, ok: false, message: '脚本 15s 内未调用 send(inited)' })
      }
    }, 15000)
    new Promise((resolve, reject) => { initedResolve = resolve; initedReject = reject })
      .then((sources) => {
        clearTimeout(timer)
        writeLine({ id: msg.id, ok: true, result: { sources: sources || initedSources || {} } })
      })
      .catch((err) => {
        clearTimeout(timer)
        writeLine({ id: msg.id, ok: false, message: String(err && err.message || err) })
      })
    try {
      runInSandbox(scriptInfo.rawScript)
    } catch (err) {
      clearTimeout(timer)
      const m = String(err && err.message || err)
      initedResolve = null; initedReject = null
      writeLine({ id: msg.id, ok: false, message: m })
    }
    return
  }
  if (msg.cmd === 'call') {
    const timer = setTimeout(() => {
      writeLine({ id: msg.id, ok: false, message: '脚本调用超时（30s）' })
    }, 30000)
    callAction(msg.source, msg.action, msg.info)
      .then((result) => { clearTimeout(timer); writeLine({ id: msg.id, ok: true, result }) })
      .catch((err) => { clearTimeout(timer); writeLine({ id: msg.id, ok: false, message: String(err && err.message || err) }) })
    return
  }
  writeLine({ id: msg.id, ok: false, message: '未知 cmd: ' + String(msg.cmd) })
})

rl.on('close', () => process.exit(0))
process.on('uncaughtException', (err) => {
  process.stderr.write(`[lx-runner] uncaught: ${err && err.stack || err}\n`)
  process.exit(1)
})
// 脚本 fire-and-forget 的 Promise（如 inited 前的 checkUpdate）rejection 没人接时，
// Node 默认按未捕获异常退出进程——桌面版宿主只是警告，这里对齐：记录不退出
process.on('unhandledRejection', (err) => {
  process.stderr.write(`[lx-runner] unhandled rejection: ${err && err.stack || err}\n`)
})
