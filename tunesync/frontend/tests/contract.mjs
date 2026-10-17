/**
 * 前端 API 层对真实后端的集成验收。
 *
 * 这不是「看契约对不对」，而是把 src/api.js 原样加载进来（只替掉 DOM 依赖和 baseURL），
 * 用前端自己的请求函数逐个打到跑着的后端上：
 * 端点不存在、方法不对、字段名不对、掩码语义不对，都会在这里炸出来。
 *
 *   node tests/contract.mjs [baseURL]
 */
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const SRC = path.join(HERE, '..', 'src', 'api.js')
const BASE = process.argv[2] || 'http://127.0.0.1:8066'

// 造一个可直接 import 的副本：只替换 DOM 相关依赖与 baseURL，逻辑一行不改。
let code = fs.readFileSync(SRC, 'utf8')
code = code.replace(
  "import { ElMessage } from 'element-plus'",
  'const ElMessage = { error() {}, success() {}, warning() {}, info() {} }')
code = code.replace("baseURL: '/api'", `baseURL: '${BASE}/api'`)
code = code.replace(/^export /gm, 'export ')
const tmp = path.join(HERE, 'api.generated.mjs')
fs.writeFileSync(tmp, code, 'utf8')

const api = await import('./api.generated.mjs?ts=' + process.hrtime.bigint())
fs.unlinkSync(tmp)

let pass = 0
const failures = []

/** 断言失败时返回一句人话，通过时返回 null。 */
async function check(name, fn, validate) {
  try {
    const data = await fn()
    const problem = validate ? validate(data) : null
    if (problem) {
      failures.push(`${name} → ${problem}`)
      console.log(`  FAIL ${name}  ${problem}`)
    } else {
      pass++
      console.log(`  ok   ${name}`)
    }
    return data
  } catch (e) {
    failures.push(`${name} → ${e.message}${detailOf(e)}`)
    console.log(`  FAIL ${name}`)
    return null
  }
}

/** axios 错误里真正有用的信息在 response.data.detail，不在 message。 */
function detailOf(e) {
  const d = e?.response?.data?.detail
  if (typeof d === 'string') return ` | detail=${d}`
  if (Array.isArray(d) && d.length) {
    return ' | detail=' + d.map((x) => `${(x.loc || []).slice(1).join('.')}: ${x.msg}`).join('；')
  }
  return ''
}

/** 断言某个请求被后端按预期拒绝（返回指定状态码且带中文 detail）。 */
async function expectRejected(name, fn, { status = 400, mustInclude } = {}) {
  try {
    await fn()
    failures.push(`${name} → 后端本应拒绝，却返回成功`)
    console.log(`  FAIL ${name}`)
    return null
  } catch (e) {
    const d = e?.response?.data?.detail
    if (e?.response?.status === status && (!mustInclude || String(d).includes(mustInclude))) {
      pass++
      console.log(`  ok   ${name}`)
      return true
    }
    failures.push(`${name} → 期望 ${status}，实际 ${e?.response?.status}${detailOf(e)}`)
    console.log(`  FAIL ${name}`)
    return null
  }
}

const isList = (d) => (Array.isArray(d) ? null : `期望数组，实际 ${typeof d}`)
const isObj = (d) => (d && typeof d === 'object' && !Array.isArray(d) ? null : `期望对象，实际 ${typeof d}`)
/** 写操作的返回体是 {ok:true} / {id,ok}，页面随后会重新拉列表。 */
const isWriteOk = (d) => (d?.ok === true ? null : `期望 ok=true，实际 ${JSON.stringify(d)?.slice(0, 80)}`)

console.log(`\n=== 前端 api.js → ${BASE}/api ===\n`)

console.log('[元信息]')
// 先备份目标库配置：后面几个用例必须改真实值才能测到（掩码语义、口令占位、
// 递归掩码），不还原就会把用户正在用的地址和口令冲掉。
const targetsBackup = await api.getTargets().catch(() => null)
await check('getCategories', api.getCategories, (d) =>
  d.length === 49 ? null : `期望 49 个分类，实际 ${d?.length}`)
await check('getPlaylistPlatforms', api.getPlaylistPlatforms, (d) =>
  d.some((p) => p.key === 'wy') ? null : '缺少 wy')
const settings = await check('getSettings', api.getSettings, (d) =>
  d.preferences && d.cron && d.cron.day_of_week.includes('0=周一')
    ? null : 'preferences / cron 约定缺失')
await check('getStatus', api.getStatus, (d) =>
  'library_files' in d && 'last_run' in d ? null : '字段不全')
await check('getLibrary', api.getLibrary, (d) =>
  'files' in d ? null : '缺 files 字段')
await check('getLatestRun', api.getLatestRun, (d) =>
  d === null ? null
    : ('id' in d && 'status' in d ? null : `结构异常：${JSON.stringify(d)?.slice(0, 80)}`))
await check('getRuns', () => api.getRuns(10), isList)
await check('getSourceHealth', api.getSourceHealth, isList)

console.log('\n[抓取偏好：前端读写服务端]')
await check('putSettings(preferences)', () => api.putSettings({
  preferences: { categories: ['study', 'sleep'], count_per: 4,
                 push_mode: 'source_llm', source_platforms: ['wy'] },
}), (d) => d.preferences?.count_per === 4 ? null : `preferences 未回显：${JSON.stringify(d.preferences)}`)
await expectRejected('putSettings 未知推送方式被拒',
  () => api.putSettings({ preferences: { push_mode: '瞎写的' } }), { mustInclude: '未知推送方式' })
await expectRejected('putSettings 未知场景被拒',
  () => api.putSettings({ preferences: { categories: ['不存在的场景'] } }),
  { mustInclude: '未知场景' })

console.log('\n[音源 CRUD]')
const srcCfg = { search: 'http://s/search?kw={kw}', url: 'http://s/u?id={id}', list_path: 'data' }
const created = await check('createSource', () =>
  api.createSource({ name: '集成测试源', priority: 3, cfg: srcCfg }),
  (d) => (d.id && d.ok === true ? null : `期望 {id, ok}，实际 ${JSON.stringify(d)}`))
const sid = created?.id
await expectRejected('createSource 空模板被拒',
  () => api.createSource({ name: '缺模板', cfg: { search: '' } }), { mustInclude: '模板' })
await expectRejected('createSource 重名冲突',
  () => api.createSource({ name: '集成测试源', cfg: srcCfg }),
  { status: 409, mustInclude: '已存在' })
if (sid) {
  // 页面保存后是重新拉列表，所以真正的断言是「重拉之后数据确实变了」
  await check('updateSource', () =>
    api.updateSource(sid, { name: '改名后的源', cfg: srcCfg }), isWriteOk)
  await check('updateSource 落库', async () => {
    const d = (await api.getSources()).find((s) => s.id === sid)
    if (!d) return '更新后列表里找不到该音源'
    if (d.name !== '改名后的源') return `名字没落库：${d.name}`
    if (d.cfg?.list_path !== 'data') return `cfg 没落库：${JSON.stringify(d.cfg)}`
    return null
  })
  await check('checkSource（连不上应返回 ok:false 而非报错）', () => api.checkSource(sid),
    (d) => d.ok === false ? null : `期望 ok=false，实际 ${JSON.stringify(d)}`)
  await check('checkAllSources', api.checkAllSources, (d) =>
    d.ok === true ? null : `期望 ok=true，实际 ${JSON.stringify(d)}`)
  await check('checkSource(404)', async () => {
    try {
      await api.checkSource(999999)
      return '后端本应 404'
    } catch (e) {
      return e?.response?.status === 404 ? null : `期望 404，实际 ${e?.response?.status}`
    }
  })
}

console.log('\n[计划任务 CRUD + 触发]')
const taskBody = {
  name: '集成测试任务', categories: ['study'], push_mode: 'source_llm',
  source_platforms: ['wy'], count_per: 2, cron_expr: '0 8 * * 7', enabled: true,
}
const task = await check('createTask', () => api.createTask(taskBody),
  (d) => (d.id && d.ok === true ? null : `期望 {id, ok}，实际 ${JSON.stringify(d)}`))
const tid = task?.id
await expectRejected('createTask 非法 cron(星期8) 被拒',
  () => api.createTask({ ...taskBody, cron_expr: '0 8 * * 8' }), { mustInclude: 'cron' })
await expectRejected('createTask 未知音源平台被拒',
  () => api.createTask({ ...taskBody, source_platforms: ['不存在的'] }), { mustInclude: '平台' })
await expectRejected('createTask 未知推送方式被拒',
  () => api.createTask({ ...taskBody, push_mode: '瞎写的' }), { mustInclude: '推送方式' })
await expectRejected('createTask 空场景列表被拒',
  () => api.createTask({ ...taskBody, categories: [] }), { mustInclude: '场景' })
if (tid) {
  await check('updateTask', () =>
    api.updateTask(tid, { ...taskBody, count_per: 5, name: '集成测试任务改名' }), isWriteOk)
  await check('updateTask 落库', async () => {
    const d = (await api.getTasks()).find((t) => t.id === tid)
    if (!d) return '更新后列表里找不到该任务'
    if (d.count_per !== 5) return `count_per 没落库：${d.count_per}`
    if (d.cron_expr !== '0 8 * * 7') return `cron 没落库：${d.cron_expr}`
    if (!d.next_run) return 'next_run 为空：cron 可能没被调度器接受'
    return null
  })
  await check('runTask', () => api.runTask(tid), (d) => d.ok === true ? null : '未返回 ok')
}

console.log('\n[立刻拉取]')

// 真跑流水线会占住并发守卫，后续断言全会被 409 挡，所以每触发一次都要等它跑完。
async function waitIdle(timeoutMs = 90000) {
  const t0 = Date.now()
  while (Date.now() - t0 < timeoutMs) {
    if (!(await api.getStatus()).fetching_now) return true
    await new Promise((r) => setTimeout(r, 400))
  }
  return false
}

await check('fetchNow 空 body 用服务端偏好', async () => {
  await api.putSettings({ preferences: { categories: ['study'], count_per: 1, push_mode: 'llm' } })
  const r = await api.fetchNow()
  if (!(await waitIdle())) return '触发后一直没有回到空闲'   // 少个 await 的话 Promise 恒真，这里根本不会等
  return r.ok === true ? null : '未返回 ok'
})

await check('fetchNow 草稿不写回偏好', async () => {
  await api.fetchNow({ categories: ['sleep'], count_per: 9 })
  if (!(await waitIdle())) return '触发后一直没有回到空闲'
  const d = await api.getSettings()
  if (d.preferences?.count_per !== 1) return `草稿把 count_per 写成了 ${d.preferences?.count_per}`
  if (JSON.stringify(d.preferences?.categories) !== JSON.stringify(['study'])) return '草稿覆盖了已保存场景'
  return null
})

// 参数校验发生在占用运行位之前，所以即使上一轮还在跑也会返回 400。
await expectRejected('fetchNow 空场景返回 400',
  () => api.fetchNow({ categories: [] }), { status: 400, mustInclude: '场景' })

await check('fetchNow 立刻再点返回 409', async () => {
  await api.fetchNow()
  // 占位是同步完成的：接口一返回就应处于「正在运行」，否则说明守卫有窗口期
  const s = await api.getStatus()
  if (s.fetching_now !== true) return '触发后 fetching_now 仍为 false'
  try {
    await expectRejected('fetchNow 并发第二次被拒',
      () => api.fetchNow(), { status: 409, mustInclude: '正在运行' })
  } finally {
    await waitIdle()
  }
  return null
})

await check('getStatus 暴露 fetching_now', async () => {
  const d = await api.getStatus()
  return typeof d.fetching_now === 'boolean' ? null : `fetching_now 类型为 ${typeof d.fetching_now}`
})

console.log('\n[目标库：掩码与 enabled]')

// 只读的部分：任何实例上都能跑
await check('getTargets 密码一律返回掩码', async () => {
  const d = await api.getTargets()
  if (d.navidrome?.password !== '***') return `navidrome.password 未掩码：${JSON.stringify(d.navidrome?.password)}`
  // 飞牛只剩直写 db 一种方式，网关那套配置不该再出现在返回体里
  if (d.feiniu?.api) return `feiniu.api 是已删除的网关配置：${JSON.stringify(d.feiniu.api)}`
  return null
})

// 改口令的部分是**不可还原**的：getTargets 只回掩码 ***，
// 真口令从来没经过 API，所以跑完无法还原回原值。
// 因此默认跳过，只有在一次性实例上才显式打开——
// 对着用户正在用的实例跑，会把 Navidrome / 飞牛的真实口令冲掉。
// 掩码语义在 Python 侧用临时库覆盖（test_api.py），不依赖这里。
const MUTATE = process.env.TS_CONTRACT_MUTATE_TARGETS === '1'
if (!MUTATE) {
  console.log('  -- 跳过 1 项「会改口令」的用例（它无法自动还原）。')
  console.log('     仅在一次性实例上用 TS_CONTRACT_MUTATE_TARGETS=1 打开。')
} else {
  await check('getTargets 顶层密码掩码', async () => {
    await api.putTargets({ navidrome: { password: '真口令' } })
    const d = await api.getTargets()
    if (d.navidrome?.password !== '***') return `navidrome.password 未掩码：${JSON.stringify(d.navidrome?.password)}`
    return null
  })
}
await check('getFeiniuSchema', api.getFeiniuSchema, (d) =>
  ('ok' in d ? null : '缺 ok 字段'))
await check('testTarget(navidrome) 失败不 500', () => api.testTarget('navidrome'),
  (d) => d.ok === false ? null : `期望 ok=false，实际 ${JSON.stringify(d)}`)

console.log('\n[本地库]')
await check('rescanLibrary', api.rescanLibrary, (d) => d.ok === true ? null : '未返回 ok')

console.log('\n[本地乐库浏览]')
await check('getLocalFacets', api.getLocalFacets, (d) =>
  ('total' in d && Array.isArray(d.artists) && Array.isArray(d.formats))
    ? null : `字段不全：${Object.keys(d || {}).join(',')}`)
await check('getLocalFiles', () => api.getLocalFiles({ limit: 5 }), (d) =>
  (Array.isArray(d.items) && 'total' in d) ? null : `结构异常：${Object.keys(d || {}).join(',')}`)
await check('getLocalFiles 关键词过滤不 500', () => api.getLocalFiles({ q: '不存在的歌' }),
  (d) => d.total === 0 ? null : `期望 0 条，实际 ${d.total}`)

console.log('\n[本地去重]')
// 只跑 dry_run：真删文件不可还原，跑在用户实例上会直接动到他的音乐库。
for (const scope of ['exact', 'song']) {
  await check(`getDuplicates(${scope})`, () => api.getDuplicates(scope), (d) =>
    (d.scope === scope && Array.isArray(d.groups) && 'redundant' in d)
      ? null : `结构异常：${JSON.stringify(d)?.slice(0, 120)}`)
}
await check('runDedupe dry_run 只统计不删', () => api.runDedupe({ dry_run: true }), (d) =>
  (d.dry_run === true && 'removed' in d) ? null : `结构异常：${JSON.stringify(d)?.slice(0, 120)}`)

console.log('\n[登录态]')
await check('getAuthState', api.getAuthState, (d) =>
  ('enabled' in d && 'authenticated' in d) ? null : `字段不全：${Object.keys(d || {}).join(',')}`)

console.log('\n[运行历史回读]')
const runs = await api.getRuns(5)
const last = await api.getLatestRun()
if (last) {
  await check(`getRun(${last.id})`, () => api.getRun(last.id), (d) =>
    Array.isArray(d.playlists) ? null : '缺 playlists')
}

console.log('\n[清理]')
if (tid) await api.deleteTask(tid).catch(() => {})
if (sid) await api.deleteSource(sid).catch(() => {})
await api.putSettings({ preferences: settings.preferences }).catch(() => {})

// 目标库的非机密字段要还原。会改口令的用例默认已跳过（见上），所以这里只需要
// 把地址 / 用户名之类改回去；**口令不动**——备份里本来就是 ***，写了反而会把
// 掩码当成新口令存进库里。
const after = await api.getTargets().catch(() => null)
if (after && targetsBackup) {
  for (const kind of ['navidrome', 'feiniu']) {
    const now = after[kind] || {}
    const was = targetsBackup[kind] || {}
    const patch = {}
    for (const [k, v] of Object.entries(was)) {
      if (k === 'password') continue
      if (v !== '' && v !== undefined && now[k] !== v) patch[k] = v
    }
    if (Object.keys(patch).length) await api.putTargets({ [kind]: patch }).catch(() => {})
  }
  console.log('  目标库的非机密配置已还原')
}

console.log(`\n${'='.repeat(52)}`)
console.log(`通过 ${pass} 项，失败 ${failures.length} 项`)
if (failures.length) {
  console.log('\n失败明细：')
  failures.forEach((f) => console.log('  - ' + f))
  process.exit(1)
}
console.log('前端 API 层与后端契约完全一致')
