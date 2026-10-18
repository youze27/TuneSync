<template>
  <div class="page">
    <div class="page-header">
      <div>
        <h2 class="page-title">刮削设置</h2>
        <div class="page-subtitle">
          生成歌单时下载会自动刮削；这里可以改刮削参数，也能对本地库手动补刮流派 / 风格 / 年份 / 封面
        </div>
      </div>
      <div class="toolbar">
        <el-button :icon="Refresh" :loading="loading" @click="load">刷新统计</el-button>
        <el-button type="primary" :icon="Check" :loading="saving" @click="save">保存刮削参数</el-button>
      </div>
    </div>

    <el-alert type="info" show-icon :closable="false" class="mb-12">
      <template #title>
        手动刮削直接改写音频文件的标签，同时把流派 / 风格 / 年份 / 专辑写回本地索引，
        <router-link to="/library">本地乐库</router-link> 的筛选就是按这些数据来的。
      </template>
    </el-alert>

    <!-- 统计压成一行：所有格子同一个字号模板，进度条宽度单独收窄 -->
    <div class="stat-strip mb-12" v-loading="loading">
      <div class="cell">
        <div class="k">索引歌曲</div>
        <div class="v">{{ stats.total }}</div>
        <div class="h">本地乐库总量</div>
      </div>
      <div class="cell">
        <div class="k">已刮削</div>
        <div class="v ok">{{ stats.done }}</div>
        <div class="h">覆盖率 {{ donePct }}</div>
      </div>
      <div class="cell">
        <div class="k">缺流派</div>
        <div class="v warn">{{ stats.no_genre }}</div>
        <div class="h">待补 genre</div>
      </div>
      <div class="cell">
        <div class="k">缺年份</div>
        <div class="v warn">{{ stats.no_year }}</div>
        <div class="h">待补 year</div>
      </div>
      <div class="cell">
        <div class="k">缺封面</div>
        <div class="v warn">{{ stats.no_cover }}</div>
        <div class="h">待补 cover</div>
      </div>
      <div class="cell">
        <div class="k">缺歌词</div>
        <div class="v warn">{{ stats.no_lrc }}</div>
        <div class="h">待补 .lrc</div>
      </div>
      <div class="cell">
        <div class="k">整体进度</div>
        <div class="bar">
          <el-progress
            :percentage="coveragePct"
            :stroke-width="10"
            :text-inside="true"
            color="#67c23a"
          />
        </div>
        <div class="h">已刮 {{ stats.done }} / {{ stats.total }} 首</div>
      </div>
      <div class="cell">
        <div class="k">本次运行</div>
        <div class="v" :class="runState.running ? 'run' : (runState.ended_at ? 'ok' : '')">
          {{ runState.running ? `${progressPct}%` : (runState.ended_at ? '已完成' : '空闲') }}
        </div>
        <div class="h">
          <template v-if="runState.running || runState.ended_at">
            成功 {{ runState.ok || 0 }} · 失败 {{ runState.failed || 0 }}
          </template>
          <template v-else>没有进行中的任务</template>
        </div>
      </div>
    </div>

    <el-row :gutter="12">
      <!-- 左：手动刮削 -->
      <el-col :xs="24" :lg="14">
        <el-card shadow="never" class="mb-12">
          <template #header>
            <div class="card-head">
              <span class="card-title"><el-icon><MagicStick /></el-icon> 手动刮削</span>
              <el-tag v-if="runState.running" type="primary" effect="dark" size="small">刮削中</el-tag>
            </div>
          </template>

          <el-form label-width="88px" label-position="left">
            <el-form-item label="刮削范围">
              <el-radio-group v-model="scope">
                <el-radio-button value="missing">只补缺失字段</el-radio-button>
                <el-radio-button value="all">全库重来</el-radio-button>
              </el-radio-group>
              <div class="muted tip">
                「只补缺失字段」优先跑流派/年份/封面/歌词还有缺口的文件，已补全的不重复跑；
                「全库重来」重新读一遍所有文件，量大时很慢
              </div>
            </el-form-item>

            <el-form-item label="数量上限">
              <el-input-number v-model="runLimit" :min="0" :max="20000" :step="100" controls-position="right" />
              <span class="muted" style="margin-left: 10px; font-size: 12px">0 = 不限，全按范围跑</span>
            </el-form-item>

            <el-form-item label="候选预览">
              <el-input-number v-model="previewLimit" :min="1" :max="1000" :step="50" controls-position="right" />
              <el-button style="margin-left: 10px" :icon="Search" :loading="loading" @click="loadCandidates">
                载入候选
              </el-button>
              <div class="muted tip">
                清单只读、先看缺口；点开始就按上面的「范围 + 上限」全部跑
              </div>
            </el-form-item>

            <el-form-item>
              <el-button
                type="primary"
                :icon="VideoPlay"
                :loading="runState.running"
                :disabled="runState.running || !runnable"
                @click="run"
              >
                {{ runState.running ? '刮削进行中…' : startLabel }}
              </el-button>
            </el-form-item>
          </el-form>

          <!-- 进度 + 详细运行详情 -->
          <div v-if="runState.running || runState.ended_at" class="run-box">
            <div class="run-head">
              <el-progress
                :percentage="progressPct"
                :status="runState.failed ? 'exception' : runState.running ? undefined : 'success'"
                :stroke-width="14"
                :format="() => `${runState.done || 0}/${runState.total || 0}`"
              />
              <div class="run-meta">
                <span class="ok">成功 {{ runState.ok || 0 }}</span>
                <span class="failed">失败 {{ runState.failed || 0 }}</span>
                <span v-if="runState.running" class="muted break-all">正在处理 {{ runState.current || '—' }}</span>
                <span v-else class="muted">
                  {{ runState.ended_at }} 结束 · 去
                  <router-link to="/library">本地乐库</router-link> 看新标签
                </span>
              </div>
            </div>

            <el-collapse v-model="openPanels" class="mt-12">
              <el-collapse-item name="detail">
                <template #title>
                  <span class="panel-title">
                    刮削明细
                    <el-tag size="small" effect="plain">{{ runLogs.length }} 条</el-tag>
                  </span>
                </template>
                <div v-if="!runLogs.length" class="muted empty-hint">还没有开始刮削</div>
                <div v-else class="log-box scroll-y">
                  <div
                    v-for="(l, i) in runLogs"
                    :key="i"
                    class="log-line"
                    :class="{ 'is-fail': !l.ok }"
                  >
                    <span class="idx">{{ l.i }}/{{ l.total }}</span>
                    <span class="mark">{{ l.ok ? '✓' : '✗' }}</span>
                    <span class="nm">{{ l.name }}<span v-if="l.artist" class="muted"> - {{ l.artist }}</span></span>
                    <span v-if="l.source" class="src">{{ srcLabel(l.source) }}</span>
                    <span v-if="l.fields?.length" class="tags">
                      <el-tag v-for="f in l.fields" :key="f" size="small" effect="plain" type="success">{{ f }}</el-tag>
                    </span>
                    <span v-else class="muted">无新增字段</span>
                    <el-button
                      v-if="l.logs?.length"
                      text
                      size="small"
                      class="more"
                      @click="toggleLog(i)"
                    >{{ opened.has(i) ? '收起' : '详情' }}</el-button>
                    <div v-if="opened.has(i)" class="sub-log">
                      <div v-for="(m, j) in l.logs" :key="j" class="sub-line">{{ m }}</div>
                      <div v-if="l.error" class="sub-line err">{{ l.error }}</div>
                    </div>
                  </div>
                </div>
              </el-collapse-item>

              <el-collapse-item v-if="runErrors.length" name="fail">
                <template #title>
                  <span class="panel-title">
                    失败清单
                    <el-tag size="small" type="danger" effect="plain">{{ runErrors.length }} 条</el-tag>
                  </span>
                </template>
                <div class="log-box scroll-y">
                  <div v-for="(e, i) in runErrors" :key="i" class="log-line is-fail">
                    <span class="break-all">{{ e.path }}</span>
                    <span class="muted">{{ e.error }}</span>
                  </div>
                </div>
              </el-collapse-item>
            </el-collapse>
          </div>
        </el-card>

        <el-card shadow="never">
          <template #header>
            <div class="card-head">
              <span class="card-title">待刮削清单</span>
              <!-- 借鉴 music-meta-web：列表上方按状态过滤，先看清缺口再决定刮哪些 -->
              <div class="head-filter">
                <el-radio-group v-model="statusFilter" size="small">
                  <el-radio-button value="all">全部 {{ candidates.length }}</el-radio-button>
                  <el-radio-button value="todo">待补 {{ countTodo }}</el-radio-button>
                  <el-radio-button value="done">已补全 {{ countDone }}</el-radio-button>
                </el-radio-group>
                <span class="muted" style="font-size: 12px">
                  {{ shownRows.length }} 条{{ candidates.length >= previewLimit ? `（预览上限 ${previewLimit}）` : '' }}
                </span>
              </div>
            </div>
          </template>

          <el-table
            v-loading="loading"
            :data="shownRows"
            row-key="path"
            size="small"
            border
            stripe
            height="calc(100vh - 620px)"
          >
            <el-table-column prop="title" label="歌名" min-width="150" show-overflow-tooltip />
            <el-table-column prop="artist" label="歌手" min-width="110" show-overflow-tooltip />
            <el-table-column label="状态" width="90" align="center">
              <template #default="{ row }">
                <el-tag v-if="row.todo" size="small" type="warning" effect="plain">待补</el-tag>
                <el-tag v-else size="small" type="success" effect="plain">已补全</el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="path" label="路径" min-width="240" show-overflow-tooltip class-name="mono" />
            <template #empty>
              <span class="muted">
                {{ stats.total ? '这个范围下没有待刮削的文件' : '本地索引还是空的，先到「本地乐库」扫描一次' }}
              </span>
            </template>
          </el-table>
        </el-card>
      </el-col>

      <!-- 右：刮削参数 -->
      <el-col :xs="24" :lg="10">
        <el-card shadow="never">
          <template #header>
            <div class="card-head">
              <span class="card-title"><el-icon><Setting /></el-icon> 刮削参数</span>
              <el-button text :icon="RefreshLeft" @click="resetForm">恢复已保存值</el-button>
            </div>
          </template>

          <el-form label-width="108px" label-position="left">
            <el-form-item label="数据源">
              <div class="src-picker">
                <el-checkbox-group v-model="form.order">
                  <el-checkbox v-for="s in SOURCES" :key="s.key" :value="s.key">
                    <span class="src-name">{{ s.name }}</span>
                    <span class="src-fields">{{ s.fields }}</span>
                  </el-checkbox>
                </el-checkbox-group>
              </div>
              <div class="muted tip">
                按勾选先后顺序依次搜索，逐字段合并：先拿到的字段不会被后面的源覆盖。
                流派与年份只有 QQ 音乐专辑接口稳定提供，建议保留它；全部取消则只靠下载时平台自带的信息
              </div>
            </el-form-item>

            <el-form-item label="内嵌封面">
              <el-switch v-model="form.embed_cover" />
              <span class="muted" style="margin-left: 10px; font-size: 12px">
                写入音频标签并在同目录生成 cover.jpg
              </span>
            </el-form-item>

            <el-form-item label="生成歌词">
              <el-switch v-model="form.save_lrc" />
              <span class="muted" style="margin-left: 10px; font-size: 12px">生成同名 .lrc 歌词文件</span>
            </el-form-item>

            <el-form-item label="并发数">
              <el-input-number v-model="form.concurrency" :min="1" :max="8" controls-position="right" />
              <div class="muted tip">同时刮几个文件。NAS 上建议 2–4，太多容易把磁盘和网络打满</div>
            </el-form-item>
          </el-form>
        </el-card>

        <el-card shadow="never" class="mt-12">
          <template #header>
            <div class="card-head">
              <span class="card-title"><el-icon><Timer /></el-icon> 定时自动刮削</span>
              <el-tag v-if="auto.enabled" size="small" type="success" effect="plain">已启用</el-tag>
              <el-tag v-else size="small" type="info" effect="plain">未启用</el-tag>
            </div>
          </template>

          <el-form label-width="108px" label-position="left">
            <el-form-item label="启用定时刮削">
              <el-switch v-model="auto.enabled" />
              <span class="muted" style="margin-left: 10px; font-size: 12px">
                启用后由后端按计划自动补刮缺失字段
              </span>
            </el-form-item>

            <el-form-item label="执行计划">
              <el-input v-model="auto.cron" placeholder="0 4 * * *" class="mono" style="max-width: 240px" />
              <div class="muted tip">每天 04:00 自动刮削缺失字段；cron 格式：分 时 日 月 周</div>
            </el-form-item>

            <el-form-item label="单次上限">
              <el-input-number v-model="auto.limit" :min="100" :max="2000" :step="100" controls-position="right" />
              <span class="muted" style="margin-left: 10px; font-size: 12px">每次定时运行最多处理的文件数</span>
            </el-form-item>
          </el-form>
        </el-card>

        <el-card shadow="never" class="mt-12">
          <template #header>
            <div class="card-head">
              <span class="card-title"><el-icon><Cpu /></el-icon> AI 元数据补全</span>
              <el-tag v-if="aiState.running" type="primary" effect="dark" size="small">进行中</el-tag>
            </div>
          </template>

          <el-form label-width="108px" label-position="left">
            <el-form-item label="工作方式">
              <div class="muted tip" style="margin-left: 0">
                用「全局设置」里配置的 LLM 批量补<b>年份 / 流派 / 风格</b>：约 20 首合成一个请求、
                按 RPM 限速控制调用频率；单首失败只记错误，不影响其他歌。只补缺失项，
                标签里已有的值不会被覆盖
              </div>
            </el-form-item>

            <el-form-item label="处理范围">
              <el-radio-group v-model="aiScope">
                <el-radio-button value="missing">只补缺失字段</el-radio-button>
                <el-radio-button value="all">全库重来</el-radio-button>
              </el-radio-group>
              <div class="muted tip">
                「全库重来」会把已有年份/流派一并送给模型复核，但已有的值仍然不会被覆盖
              </div>
            </el-form-item>

            <el-form-item label="数量上限">
              <el-input-number v-model="aiLimit" :min="0" :max="20000" :step="100" controls-position="right" />
              <span class="muted" style="margin-left: 10px; font-size: 12px">0 = 不限，全按范围跑</span>
            </el-form-item>

            <el-form-item>
              <el-button
                type="primary"
                :icon="Cpu"
                :loading="aiState.running"
                :disabled="aiState.running"
                @click="runAi"
              >
                {{ aiState.running ? `AI 补全中 ${aiState.done || 0}/${aiState.total || 0}` : '开始 AI 补全' }}
              </el-button>
            </el-form-item>
          </el-form>

          <div v-if="aiState.running || aiState.ended_at" class="run-box">
            <div class="run-head">
              <el-progress
                :percentage="aiPct"
                :status="aiState.failed ? 'exception' : aiState.running ? undefined : 'success'"
                :stroke-width="14"
                :format="() => `${aiState.done || 0}/${aiState.total || 0}`"
              />
              <div class="run-meta">
                <span class="ok">成功 {{ aiState.ok || 0 }}</span>
                <span class="failed">失败 {{ aiState.failed || 0 }}</span>
                <span v-if="!aiState.running && aiState.ended_at" class="muted">
                  {{ aiState.ended_at }} 结束 · 去
                  <router-link to="/library">本地乐库</router-link> 看新标签
                </span>
              </div>
            </div>

            <el-collapse v-if="aiErrors.length" v-model="aiOpen" class="mt-12">
              <el-collapse-item name="ai-fail">
                <template #title>
                  <span class="panel-title">
                    失败清单
                    <el-tag size="small" type="danger" effect="plain">{{ aiErrors.length }} 条</el-tag>
                  </span>
                </template>
                <div class="log-box scroll-y">
                  <div v-for="(e, i) in aiErrors" :key="i" class="log-line is-fail">
                    <span class="break-all">{{ e.path || '（整批请求失败）' }}</span>
                    <span class="muted">{{ e.error }}</span>
                  </div>
                </div>
              </el-collapse-item>
            </el-collapse>
          </div>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  Check, Cpu, MagicStick, Refresh, RefreshLeft, Search, Setting, Timer, VideoPlay,
} from '@element-plus/icons-vue'
import * as api from '../api.js'

const loading = ref(false)
const saving = ref(false)

// ───────────────────────────── 统计概览

const stats = reactive({ total: 0, done: 0, no_genre: 0, no_year: 0, no_cover: 0, no_lrc: 0 })

const donePct = computed(() => {
  if (!stats.total) return '—'
  return `${Math.round((stats.done / stats.total) * 100)}%`
})

/** 顶部进度条用的数值覆盖率；没有数据时给 0 免得进度条抖动。 */
const coveragePct = computed(() => {
  if (!stats.total) return 0
  return Math.min(100, Math.round((stats.done / stats.total) * 100))
})

// ───────────────────────────── 刮削参数

/** 可勾选的数据源。fields 直接标注每个源实际能拿到什么，避免勾了才发现缺字段。 */
const SOURCES = [
  { key: 'wy', name: '网易云音乐', fields: '专辑 / 年份 / 封面 / 歌词' },
  { key: 'tx', name: 'QQ音乐', fields: '专辑 / 流派 / 年份 / 封面 / 歌词' },
  { key: 'kg', name: '酷狗音乐', fields: '专辑 / 封面 / 歌词' },
  { key: 'kw', name: '酷我音乐', fields: '专辑' },
  { key: 'ap', name: 'Apple Music', fields: '专辑 / 流派 / 年份 / 封面' },
  { key: 'mb', name: 'MusicBrainz 开放库', fields: '专辑 / 流派 / 年份 / 封面（欧美日韩兜底）' },
]
const SRC_NAME = Object.fromEntries(SOURCES.map((s) => [s.key, s.name]))
const srcLabel = (k) => SRC_NAME[k] || k

const DEFAULT_ORDER = ['wy', 'tx', 'kg', 'kw', 'ap', 'mb']

const form = reactive({
  order: [...DEFAULT_ORDER],
  embed_cover: true,
  save_lrc: true,
  concurrency: 6,
})

/** 定时自动刮削：嵌套在 scraper.auto_scrape 里原样透传给后端。 */
const auto = reactive({ enabled: false, cron: '0 4 * * *', limit: 500 })

/** 后端没有 scraper 字段白名单，这里只挑认识的键回填。 */
function fillForm(s = {}) {
  const sc = s.scraper || {}
  const picked = (Array.isArray(sc.order) ? sc.order : [])
    .map((x) => String(x).toLowerCase())
    .filter((x) => SOURCES.some((v) => v.key === x))
  form.order = picked.length ? picked : [...DEFAULT_ORDER]
  form.embed_cover = sc.embed_cover ?? true
  form.save_lrc = sc.save_lrc ?? true
  form.concurrency = sc.concurrency ?? 6
  const as = sc.auto_scrape || {}
  auto.enabled = !!as.enabled
  auto.cron = as.cron || '0 4 * * *'
  auto.limit = Number(as.limit) || 500
  // 运行选项也持久化在 scraper 节里，换浏览器/设备不丢
  scope.value = sc.manual_scope === 'all' ? 'all' : 'missing'
  runLimit.value = Number(sc.manual_limit) || 0
  previewLimit.value = Number(sc.preview_limit) || 200
}

/** 保存载荷：参数 + 运行选项一起入库，保证「所见即所跑」。 */
function paramsPayload() {
  return {
    scraper: {
      order: form.order,
      embed_cover: form.embed_cover,
      save_lrc: form.save_lrc,
      concurrency: form.concurrency,
      manual_scope: scope.value,
      manual_limit: runLimit.value,
      preview_limit: previewLimit.value,
      auto_scrape: { enabled: auto.enabled, cron: auto.cron, limit: auto.limit },
    },
  }
}

async function save() {
  saving.value = true
  try {
    const res = await api.putSettings(paramsPayload())
    fillForm(res)
    ElMessage.success('刮削参数已保存')
  } catch (e) {
    api.toastError(e, '保存刮削参数失败')
  } finally {
    saving.value = false
  }
}

async function resetForm() {
  try {
    fillForm(await api.getSettings())
    ElMessage.success('已恢复为服务端保存的值')
  } catch (e) {
    api.toastError(e, '读取已保存的刮削参数失败')
  }
}

// ───────────────────────────── 手动刮削

const scope = ref('missing')
const runLimit = ref(0)
const previewLimit = ref(200)
const candidates = ref([])

// 清单状态过滤（借鉴 music-meta-web 的顶部过滤器），纯前端切片，不重新请求
// todo 由后端按「缺任一字段」算好，与统计口径同源，避免两边对不上
const statusFilter = ref('all')
const countTodo = computed(() => candidates.value.filter((r) => r.todo).length)
const countDone = computed(() => candidates.value.length - countTodo.value)
const shownRows = computed(() => {
  if (statusFilter.value === 'todo') return candidates.value.filter((r) => r.todo)
  if (statusFilter.value === 'done') return candidates.value.filter((r) => !r.todo)
  return candidates.value
})

// 进度：轮询 /scrape/status 拿，逐首明细在 logs 里
const runState = reactive({
  running: false, done: 0, total: 0, ok: 0, failed: 0, current: '', ended_at: null,
})
const runErrors = ref([])
const runLogs = ref([])
const openPanels = ref(['detail'])
const opened = ref(new Set())

function toggleLog(i) {
  const next = new Set(opened.value)
  next.has(i) ? next.delete(i) : next.add(i)
  opened.value = next
}

function applyStatus(s) {
  if (!s) return
  runState.running = !!s.running
  runState.done = s.done || 0
  runState.total = s.total || 0
  runState.ok = s.ok || 0
  runState.failed = s.failed || 0
  runState.current = s.current || ''
  runState.ended_at = s.ended_at || null
  runErrors.value = s.errors || []
  runLogs.value = s.logs || []
}

const runnable = computed(() => stats.total > 0)

const progressPct = computed(() => {
  if (!runState.total) return 0
  return Math.min(100, Math.round((runState.done / runState.total) * 100))
})

const startLabel = computed(() => {
  if (runLimit.value) return `刮削 ${runLimit.value} 首`
  return scope.value === 'all' ? '刮削全库' : '刮削全部待补文件'
})

async function loadCandidates() {
  loading.value = true
  try {
    const res = await api.getScrapeCandidates({ scope: scope.value, limit: previewLimit.value })
    candidates.value = res?.items || []
    applyStats(res?.stats)
  } catch (e) {
    api.toastError(e, '载入待刮削清单失败')
  } finally {
    loading.value = false
  }
}

async function run() {
  if (scope.value === 'all') {
    try {
      await ElMessageBox.confirm(
        '「全库重来」会重新读一遍所有文件的标签，量大时很慢。确定继续？',
        '确认全库重刮',
        { type: 'warning', confirmButtonText: '确定刮削', cancelButtonText: '取消' },
      )
    } catch {
      return
    }
  }
  // 先把界面上的参数与运行选项存库再开跑，保证这次任务严格按当前配置执行；
  // 保存失败就别跑，否则跑完用户会发现「跟配的不一样」
  saving.value = true
  try {
    await api.putSettings(paramsPayload())
  } catch (e) {
    api.toastError(e, '保存刮削参数失败，已取消本次刮削')
    saving.value = false
    return
  } finally {
    saving.value = false
  }
  loading.value = true
  try {
    await api.runScrape({ scope: scope.value, limit: runLimit.value })
    // 先乐观标成运行中：刮削很快时，第一次轮询可能就已经结束了
    Object.assign(runState, {
      running: true, done: 0, ok: 0, failed: 0, ended_at: null,
    })
    runErrors.value = []
    runLogs.value = []
    opened.value = new Set()
    startPolling()
    ElMessage.success('已开始刮削，进度见下方')
  } catch (e) {
    api.toastError(e, '启动刮削失败')
  } finally {
    loading.value = false
  }
}

// ───────────────────────────── 进度轮询

let timer = null
let pollFails = 0

function poll() {
  api
    .getScrapeStatus()
    .then((s) => {
      pollFails = 0
      const finished = runState.running && !s?.running
      applyStatus(s)
      // 刚跑完：概览和候选清单都变了，拉一次最新的
      if (finished) {
        stopPolling()
        // 后端空任务兜底会把 total 落成 0，这里明确告诉用户为什么没动静
        if (!runState.total) ElMessage.warning('没有可刮削的文件：没有找到待补字段的曲目')
        load()
      }
    })
    .catch(() => {
      // 一次网络抖动就停轮询，任务会看起来「没执行」；连续失败几次才放弃
      pollFails += 1
      if (pollFails >= 4) stopPolling()
    })
}

function startPolling() {
  stopPolling()
  pollFails = 0
  poll()
  timer = setInterval(poll, 1500)
}

function stopPolling() {
  clearInterval(timer)
  timer = null
}

onMounted(() => load())
onUnmounted(() => stopPolling())

// ───────────────────────────── 载入

function applyStats(s) {
  if (!s) return
  stats.total = s.total || 0
  stats.done = s.done || 0
  stats.no_genre = s.no_genre || 0
  stats.no_year = s.no_year || 0
  stats.no_cover = s.no_cover || 0
  stats.no_lrc = s.no_lrc || 0
}

async function load() {
  loading.value = true
  try {
    const [st, run] = await Promise.all([api.getScrapeStats(), api.getScrapeStatus()])
    applyStats(st)
    applyStatus(run)
    if (run?.running) startPolling()
    // 参数与候选一起拿：进页面就该看到当前配置和可刮的曲子
    const [s] = await Promise.all([api.getSettings(), loadCandidates()])
    fillForm(s)
  } catch (e) {
    api.toastError(e, '加载刮削数据失败')
  } finally {
    loading.value = false
  }
}

// ───────────────────────────── AI 元数据补全

const aiScope = ref('missing')
const aiLimit = ref(0)
const aiOpen = ref('')
const aiState = reactive({
  running: false, done: 0, total: 0, ok: 0, failed: 0, ended_at: null, errors: [],
})
const aiErrors = computed(() => aiState.errors || [])
const aiPct = computed(() => {
  if (!aiState.total) return 0
  return Math.min(100, Math.round(((aiState.done || 0) / aiState.total) * 100))
})

let aiTimer = null

async function pollAi() {
  try {
    const s = await api.getScrapeAiStatus()
    Object.assign(aiState, s, { errors: s.errors || [] })
    if (!s.running && aiTimer) {
      clearInterval(aiTimer)
      aiTimer = null
      load()                     // 跑完刷一次统计，覆盖率立即可见
    }
  } catch {
    /* 轮询失败下轮再说 */
  }
}

async function runAi() {
  try {
    await ElMessageBox.confirm(
      '将用 LLM 批量补年份 / 流派 / 风格并写入文件标签，耗时取决于库的大小与 RPM 限制。确认开始？',
      'AI 元数据补全',
      { type: 'warning', confirmButtonText: '开始', cancelButtonText: '再想想' },
    )
  } catch {
    return
  }
  try {
    const s = await api.runScrapeAi({ scope: aiScope.value, limit: aiLimit.value })
    Object.assign(aiState, s, { errors: s.errors || [] })
    if (!aiTimer) aiTimer = setInterval(pollAi, 2000)
    ElMessage.success('AI 补全已开始，进度见下方')
  } catch (e) {
    api.toastError(e, '启动 AI 补全失败')
  }
}

// 进页面先问一次：容器重启后浏览器不知道有任务在跑，靠这里接上
onMounted(() => {
  pollAi().then(() => {
    if (aiState.running && !aiTimer) aiTimer = setInterval(pollAi, 2000)
  })
})
onUnmounted(() => {
  if (aiTimer) clearInterval(aiTimer)
})
</script>

<style scoped>
.card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}
.head-filter {
  display: flex;
  align-items: center;
  gap: 10px;
}
.card-title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-weight: 600;
  font-size: 14px;
}
.tip {
  font-size: 12px;
  margin-top: 4px;
}
/* 统计条：等宽 grid，每个格子共用同一套字号（标题 13 / 数值 20 / 辅助 12） */
.stat-strip {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(128px, 1fr));
  gap: 2px;
  padding: 12px 4px;
  background: var(--el-bg-color);
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
}
.stat-strip .cell {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
  padding: 0 14px;
  border-right: 1px solid var(--el-border-color-lighter);
}
.stat-strip .cell:last-child {
  border-right: none;
}
.stat-strip .k {
  font-size: 13px;
  line-height: 18px;
  color: var(--el-text-color-secondary);
  white-space: nowrap;
}
.stat-strip .v {
  font-size: 20px;
  font-weight: 600;
  line-height: 26px;
  color: var(--el-text-color-primary);
  font-variant-numeric: tabular-nums;
}
.stat-strip .v.ok {
  color: #67c23a;
}
.stat-strip .v.warn {
  color: #e6a23c;
}
.stat-strip .v.run {
  color: #409eff;
}
.stat-strip .h {
  font-size: 12px;
  line-height: 16px;
  color: var(--el-text-color-placeholder);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
/* 进度条单独收窄，不再横着吃掉整行 */
.stat-strip .bar {
  width: 100%;
  max-width: 128px;
}
@media (max-width: 1100px) {
  .stat-strip .cell {
    border-right: none;
  }
}
.run-box {
  border-top: 1px dashed #ebeef5;
  padding-top: 12px;
}
.run-head {
  min-width: 0;
}
.run-meta {
  display: flex;
  align-items: center;
  gap: 14px;
  flex-wrap: wrap;
  font-size: 12px;
  margin-top: 8px;
}
.run-meta .ok {
  color: #67c23a;
  font-weight: 600;
}
.run-meta .failed {
  color: #f56c6c;
  font-weight: 600;
}
.panel-title {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  font-weight: 600;
}
.empty-hint {
  font-size: 12px;
  padding: 6px 0;
}
/* 逐首明细：单行排布，字段多就换行，不再靠长路径把行撑开 */
.log-box {
  max-height: 320px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  padding: 6px 8px;
  background: var(--el-fill-color-lighter);
}
.log-box .log-line {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  font-size: 12px;
  line-height: 20px;
  padding: 2px 0;
  border-bottom: 1px dashed var(--el-border-color-extra-light);
}
.log-box .log-line:last-child {
  border-bottom: none;
}
.log-box .log-line.is-fail {
  background: var(--el-color-danger-light-9);
}
.log-box .idx {
  color: var(--el-text-color-placeholder);
  font-variant-numeric: tabular-nums;
  min-width: 54px;
}
.log-box .mark {
  font-weight: 700;
  color: #67c23a;
}
.log-box .is-fail .mark {
  color: #f56c6c;
}
.log-box .nm {
  font-weight: 500;
  color: var(--el-text-color-primary);
}
.log-box .src {
  font-size: 11px;
  padding: 0 6px;
  border-radius: 3px;
  background: var(--el-color-primary-light-9);
  color: var(--el-color-primary);
}
.log-box .tags {
  display: inline-flex;
  flex-wrap: wrap;
  gap: 4px;
}
.log-box .more {
  padding: 0;
  height: 20px;
}
.sub-log {
  flex-basis: 100%;
  margin: 2px 0 6px 62px;
  padding: 4px 8px;
  border-left: 2px solid var(--el-border-color);
  font-size: 11px;
  line-height: 18px;
  color: var(--el-text-color-secondary);
  background: var(--el-bg-color);
}
.sub-log .err {
  color: #f56c6c;
}
/* 数据源勾选：名称与可提供字段上下两行，一眼看出勾了能补什么 */
.src-picker {
  display: flex;
  width: 100%;
}
.src-picker :deep(.el-checkbox-group) {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.src-picker :deep(.el-checkbox) {
  height: auto;
  margin-right: 0;
  align-items: flex-start;
}
.src-name {
  font-weight: 500;
}
.src-fields {
  display: block;
  font-size: 11px;
  color: var(--el-text-color-placeholder);
  line-height: 16px;
}
</style>
