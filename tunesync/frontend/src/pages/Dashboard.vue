<template>
  <div class="page" v-loading="state.loading">
    <el-alert
      v-if="state.error"
      :title="state.error"
      type="error"
      show-icon
      class="mb-12"
      @close="state.error = ''"
    />

    <!-- 没有任何计划任务时的引导 -->
    <el-card v-if="bootedEmpty" shadow="never" class="mb-12 guide-card">
      <div class="guide-inner">
        <el-icon :size="40" color="#409eff"><Calendar /></el-icon>
        <div class="guide-text">
          <div class="guide-title">还没有计划任务</div>
          <div class="guide-sub">
            TuneSync 需要一个推送计划才会开始推送。进「推送计划」新建一条，把想推的场景、方式和频率都填在同一步里。
          </div>
        </div>
        <div class="toolbar">
          <el-button type="primary" :icon="Plus" @click="go('/tasks')">去创建推送计划</el-button>
        </div>
      </div>
    </el-card>

    <!-- 统计卡 -->
    <div class="stat-grid mb-12">
      <StatCard
        label="最近推送"
        :value="lastRun ? fmtAgo(lastRun.finished_at || lastRun.started_at) : '从未运行'"
        :icon="'Clock'"
        :color="lastRun ? runTag(lastRun.status).color : '#909399'"
        :hint="lastRun ? `${fmtTime(lastRun.started_at)} · ${runTag(lastRun.status).label}${summary.total ? ' · 推送 ' + rateText(summary) : ''}` : '尚未执行过推送'"
      >
        <template #extra>
          <el-tag v-if="lastRun" size="small" :type="runTag(lastRun.status).type">
            {{ runTag(lastRun.status).label }}
          </el-tag>
          <span v-else class="muted">尚未执行过推送</span>
        </template>
      </StatCard>

      <StatCard
        label="歌单总数"
        :value="state.status?.playlists_total ?? '—'"
        icon="Files"
        color="#409eff"
        hint="历史推送成功累计"
      />
      <StatCard
        label="本地复用"
        :value="summary.local"
        icon="FolderOpened"
        color="#67c23a"
        :hint="`下载 ${summary.downloaded} 首 · 跳过 ${summary.skipped} 首`"
      />
      <StatCard
        label="本地库文件"
        :value="state.library.files"
        icon="FolderChecked"
        color="#909399"
        :hint="libraryHint"
      >
        <template #extra>
          <el-button size="small" text type="primary" :loading="rescanLoading" @click="rescan">
            立即扫描本地库
          </el-button>
        </template>
      </StatCard>
    </div>

    <el-row :gutter="12">
      <!-- 左列：实时进度 + 最近一次运行概况 -->
      <el-col :xs="24" :lg="14">
        <el-card shadow="never" class="mb-12">
          <template #header>
            <div class="card-head">
              <span class="card-title">
                <el-icon><Odometer /></el-icon> 运行进度
              </span>
              <el-tag v-if="anyRunning" type="primary" effect="dark" size="small">运行中</el-tag>
              <el-tag v-if="state.queue.depth" type="warning" effect="plain" size="small">
                排队 {{ state.queue.depth }}
              </el-tag>
              <el-tag v-else-if="live.runId" size="small" effect="plain">空闲</el-tag>
              <el-tag v-else size="small" type="info" effect="plain">暂无运行</el-tag>
            </div>
          </template>

          <!-- 推送全局串行：正在跑的排最前（active），后面按等待顺序列出 -->
          <div v-if="queueRows.length" class="queue-box mb-12">
            <div class="queue-head">
              <span class="muted">运行队列</span>
              <span class="muted">共 {{ queueRows.length }} 个</span>
            </div>
            <div
              v-for="(q, i) in queueRows"
              :key="q.guard"
              class="run-row"
              :class="{ on: q.active }"
            >
              <span class="tr-title">#{{ i + 1 }}</span>
              <el-tag v-if="q.active" size="small" type="primary" effect="dark">运行中</el-tag>
              <el-tag v-else size="small" type="warning" effect="plain">等待中</el-tag>
              <span class="tr-title">{{ q.label }}</span>
              <span v-if="q.since" class="muted">{{ sinceText(q.since) }}</span>
            </div>
          </div>

          <!--
            多个任务同时在页面上时按 run 分行展示：队列是串行的，但排队期间前一个的
            进度还没被看过，直接覆盖就丢了。每行点一下切换下面的明细。
          -->
          <div v-if="allRuns.length > 1" class="multi-run mb-12">
            <div
              v-for="r in allRuns"
              :key="r.runId"
              class="run-row"
              :class="{ on: r.runId === state.liveId }"
              @click="focusRun(r.runId)"
            >
              <span class="tr-title">#{{ r.runId }} {{ r.taskName || '—' }}</span>
              <el-tag v-if="r.active" size="small" type="primary">运行中</el-tag>
              <el-tag v-else-if="r.ended" size="small" type="success">已结束</el-tag>
              <el-tag v-else size="small" type="info">待运行</el-tag>
              <span class="muted">
                本地 {{ r.totals.local }} · 下载 {{ r.totals.downloaded }} · 跳过 {{ r.totals.skipped }}
              </span>
            </div>
          </div>

          <div v-if="live.runId" class="live-head">
            <span>任务：<b>{{ live.taskName || '—' }}</b></span>
            <el-divider direction="vertical" />
            <span>运行 #{{ live.runId }}</span>
            <el-divider direction="vertical" />
            <span class="muted">{{ live.startedAt }}</span>
          </div>

          <el-progress
            :percentage="progressPercent"
            :status="progressStatus"
            :indeterminate="live.active && !live.ended"
            :show-text="!live.active"
            :stroke-width="12"
            striped
            :striped-flow="live.active"
          />

          <div class="live-counts">
            <el-tag type="success" size="small">本地复用 {{ live.totals.local }}</el-tag>
            <el-tag type="primary" size="small">已下载 {{ live.totals.downloaded }}</el-tag>
            <el-tag type="info" size="small">已跳过 {{ live.totals.skipped }}</el-tag>
            <el-tag v-if="state.status?.active_jobs" type="warning" size="small">
              待处理任务 {{ state.status.active_jobs }}
            </el-tag>
          </div>

          <el-tabs v-model="liveTab" class="live-tabs">
            <el-tab-pane :label="`歌曲推送（${live.tracks.length}）`">
              <div ref="trackBox" class="log-box scroll-y">
                <div v-if="!live.tracks.length" class="empty-hint">暂无歌曲进度</div>
                <div v-for="(t, i) in live.tracks" :key="i" class="log-line">
                  <span class="muted">{{ t.at }}</span>
                  <el-tag size="small" :type="stTag(t.status).type">{{ stTag(t.status).label }}</el-tag>
                  <span class="tr-title">{{ t.title }}</span>
                  <span class="muted tr-artist">{{ t.artist }}</span>
                  <span v-if="t.source" class="muted tr-src">{{ t.source }}</span>
                  <span v-if="t.error" class="tr-err">{{ t.error }}</span>
                </div>
              </div>
            </el-tab-pane>

            <el-tab-pane :label="`阶段日志（${live.progress.length}）`">
              <div ref="progBox" class="log-box scroll-y">
                <div v-if="!live.progress.length" class="empty-hint">暂无阶段日志</div>
                <div v-for="(p, i) in live.progress" :key="i" class="log-line">
                  <span class="muted">{{ p.at }}</span>
                  <el-tag size="small" :type="p.stage === 'fetch-error' ? 'danger' : 'info'">
                    {{ stageLabel(p.stage) }}
                  </el-tag>
                  <span class="tr-title">{{ p.detail }}</span>
                </div>
              </div>
            </el-tab-pane>
          </el-tabs>
        </el-card>

        <el-card shadow="never">
          <template #header>
            <div class="card-head">
              <span class="card-title"><el-icon><DataAnalysis /></el-icon> 最近一次运行概况</span>
              <el-button v-if="lastRun" size="small" text type="primary" @click="go('/runs')">
                查看完整历史
              </el-button>
            </div>
          </template>

          <template v-if="lastRunDetail">
            <div class="run-line">
              运行 #{{ lastRunDetail.id }} · 任务 {{ lastRunDetail.task_name || '—' }} ·
              {{ fmtTime(lastRunDetail.started_at) }} · 耗时
              {{ fmtDuration(lastRunDetail.started_at, lastRunDetail.finished_at) }}
              <template v-if="summary.total">
                · 推送成功率 {{ rateText(summary) }}（{{ ratePct(summary) }}）
              </template>
              <el-tag size="small" :type="runTag(lastRunDetail.status).type" style="margin-left: 6px">
                {{ runTag(lastRunDetail.status).label }}
              </el-tag>
            </div>

            <el-alert
              v-if="summary.fatal"
              :title="`运行中断：${summary.fatal}`"
              type="error"
              show-icon
              :closable="false"
              class="mb-12"
            />

            <el-table :data="platformMatrix" size="small" border class="mb-12">
              <el-table-column prop="platform" label="来源" width="110" />
              <el-table-column prop="category" label="场景" width="110" />
              <el-table-column prop="count" label="生成歌单数" width="110" align="center" />
              <el-table-column prop="tracks" label="曲目合计" width="100" align="center" />
              <el-table-column label="本地/下载/跳过" min-width="140">
                <template #default="{ row }">
                  <span class="success">{{ row.local }}</span> /
                  <span class="primary">{{ row.downloaded }}</span> /
                  <span class="muted">{{ row.skipped }}</span>
                </template>
              </el-table-column>
            </el-table>

            <div v-if="summary.errors.length || failedPlaylists.length" class="error-box">
              <div class="error-title">
                <el-icon><WarningFilled /></el-icon> 失败原因（{{ summary.errors.length + failedPlaylists.length }}）
              </div>
              <ul>
                <li v-for="(e, i) in summary.errors" :key="`s${i}`">{{ e }}</li>
                <li v-for="p in failedPlaylists" :key="`p${p.id}`">
                  [{{ platName(p.platform) }} / {{ catName(p.category) }}] {{ p.name }}：{{ p.error || p.status }}
                </li>
              </ul>
            </div>
            <div v-else class="muted">本次运行没有失败项。</div>
          </template>

          <div v-else class="empty-hint">还没有任何运行记录。</div>
        </el-card>
      </el-col>

      <!-- 右列：音源健康 -->
      <el-col :xs="24" :lg="10">
        <el-card shadow="never">
          <template #header>
            <div class="card-head">
              <span class="card-title"><el-icon><Headset /></el-icon> 音源健康状态</span>
              <el-button
                size="small"
                type="primary"
                plain
                :icon="Refresh"
                :loading="checkingAll"
                @click="checkAll"
              >
                全部检测
              </el-button>
            </div>
          </template>

          <el-table :data="sortedSources" size="small" border>
            <el-table-column label="优先级" width="70" align="center">
              <template #default="{ row }">{{ row.priority }}</template>
            </el-table-column>
            <el-table-column prop="name" label="音源" min-width="120" show-overflow-tooltip />
            <el-table-column label="健康" width="90">
              <template #default="{ row }"><HealthTag :health="row.health" /></template>
            </el-table-column>
            <el-table-column label="失败次数" width="80" align="center">
              <template #default="{ row }">{{ row.fail_count ?? 0 }}</template>
            </el-table-column>
            <el-table-column label="成功率" width="110" align="center">
              <template #default="{ row }">{{ successRateText(row) }}</template>
            </el-table-column>
            <el-table-column label="最近检测" width="110" align="center">
              <template #default="{ row }">{{ fmtAgo(row.last_check) }}</template>
            </el-table-column>
            <el-table-column prop="last_error" label="最近错误" min-width="160">
              <template #default="{ row }">
                <el-tooltip
                  v-if="row.last_error"
                  :content="row.last_error"
                  placement="top"
                  :show-after="300"
                >
                  <span class="err-text err-ellipsis">{{ row.last_error }}</span>
                </el-tooltip>
                <span v-else class="muted">—</span>
              </template>
            </el-table-column>
          </el-table>

          <div class="health-legend">
            <el-tag size="small" type="success" effect="plain">healthy 正常</el-tag>
            <el-tag size="small" type="warning" effect="plain">degraded 降级</el-tag>
            <el-tag size="small" type="danger" effect="plain">down 已熔断</el-tag>
            <span class="muted" style="margin-left: 8px">熔断的音源会被自动跳过</span>
          </div>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<script>
export default { name: 'Dashboard' }
</script>

<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Calendar, Clock, DataAnalysis, FolderChecked, FolderOpened, Files, Headset, Odometer, Plus, Refresh, WarningFilled } from '@element-plus/icons-vue'
import * as api from '../api.js'
import HealthTag from '../components/HealthTag.vue'
import StatCard from '../components/StatCard.vue'
import { allRuns, anyRunning, catName, liveRun, refreshDashboard, stageLabel, state } from '../store.js'
import { fmtAgo, fmtDuration, fmtTime, runStatusTag, summaryOf, trackStatusTag } from '../utils.js'

const router = useRouter()
const go = (p) => router.push(p)

// 还没有任何运行时 liveRun 是 null，模板里到处在读 live.totals.*，
// 用一个全零的空壳兜住，省得每个属性都写 ?.。
const EMPTY_LIVE = {
  runId: null, taskName: '', active: false, startedAt: '',
  progress: [], tracks: [], totals: { local: 0, downloaded: 0, skipped: 0 }, ended: null,
}
const live = computed(() => liveRun.value || EMPTY_LIVE)
const focusRun = (runId) => { state.liveId = runId }
const rescanLoading = ref(false)
const checkingAll = ref(false)
const trackBox = ref(null)
const progBox = ref(null)
const liveTab = ref('tracks')

/**
 * 队列视图：后端 active 在前、waiting 在后，
 * 拼成一张完整的表（正在跑的第一个），每项带上已经等了多久。
 */
const queueRows = computed(() => {
  const q = state.queue
  const rows = []
  if (q.active) rows.push({ ...q.active, active: true })
  for (const w of q.waiting || []) rows.push({ ...w, active: false })
  return rows
})

/** 「已等待 2 分 10 秒」这种，秒级刷新由 clock 驱动。 */
const clock = ref(0)
const sinceText = (since) => {
  const t = new Date(since).getTime()
  if (Number.isNaN(t)) return ''
  const sec = Math.max(0, Math.floor((clock.value - t) / 1000))
  if (sec < 60) return `已等待 ${sec} 秒`
  if (sec < 3600) return `已等待 ${Math.floor(sec / 60)} 分 ${sec % 60} 秒`
  return `已等待 ${Math.floor(sec / 3600)} 小时 ${Math.floor((sec % 3600) / 60)} 分`
}

const lastRun = computed(() => state.status?.last_run || null)
const lastRunDetail = computed(() => state.lastRunDetail)
const summary = computed(() => summaryOf(lastRun.value))
const sources = computed(() => state.status?.sources || [])

/** 累计成功率：ok+err 都为 0 视为未知（返回 null，排最后）。 */
function successRate(row) {
  const ok = Number(row.ok_count) || 0
  const err = Number(row.err_count) || 0
  if (!ok && !err) return null
  return ok / (ok + err)
}

function successRateText(row) {
  const ok = Number(row.ok_count) || 0
  const err = Number(row.err_count) || 0
  if (!ok && !err) return '—'
  return `${Math.round((ok / (ok + err)) * 100)}% (${ok}/${err})`
}

/** 音源健康表按成功率从高到低渲染，成功率未知（还没下载过）的排最后。 */
const sortedSources = computed(() =>
  [...sources.value].sort((a, b) => {
    const ra = successRate(a)
    const rb = successRate(b)
    if (ra === null && rb === null) return (a.priority ?? 0) - (b.priority ?? 0)
    if (ra === null) return 1
    if (rb === null) return -1
    return rb - ra
  }),
)

const bootedEmpty = computed(() => state.booted && state.tasks.length === 0)

const stTag = (s) => trackStatusTag(s)
const runTag = (s) => runStatusTag(s)
// 平台抓取已删：歌单来源固定是 LLM，其余历史值原样显示
const PLATFORM_NAMES = { llm: 'LLM' }
const platName = (code) => PLATFORM_NAMES[code] || code || '未知'
const catLabel = catName

const libraryHint = computed(() =>
  state.library.scanned_at ? `索引于 ${fmtAgo(state.library.scanned_at)}` : '尚未建立索引',
)

/** 成功率：「推送成功/歌单总歌曲」 */
const rateText = (x) => `${x.pushed}/${x.total}`
const ratePct = (x) => (x.total ? `${Math.round((x.pushed / x.total) * 100)}%` : '—')

/** 平台 × 分类 聚合（来自 /api/runs/latest 的 playlists）。 */
const platformMatrix = computed(() => {
  const pls = lastRunDetail.value?.playlists || []
  const map = new Map()
  for (const p of pls) {
    const key = `${p.platform}||${p.category}`
    if (!map.has(key)) {
      map.set(key, {
        platform: platName(p.platform),
        category: catLabel(p.category),
        count: 0,
        tracks: 0,
        local: 0,
        downloaded: 0,
        skipped: 0,
      })
    }
    const row = map.get(key)
    row.count += 1
    row.tracks += p.track_total || 0
    row.local += p.local_hit || 0
    row.downloaded += p.downloaded || 0
    row.skipped += p.skipped || 0
  }
  return [...map.values()].sort((a, b) => b.count - a.count)
})

const failedPlaylists = computed(() =>
  (lastRunDetail.value?.playlists || []).filter((p) => p.error || p.status === 'failed' || p.status === 'error'),
)

/**
 * 后端不提供总进度（既没有总曲目数也没有百分比），
 * 运行中就交给 indeterminate 的条纹动画表示「在跑」，不再拿已处理首数当百分比
 * ——处理 30 首就显示 30% 是彻头彻尾的假进度。结束后才是确定的 100%。
 */
const progressPercent = computed(() => {
  const r = live.value
  if (!r.runId) return 0
  return r.active ? 0 : 100
})

const progressStatus = computed(() => {
  const r = live.value
  if (r.active) return undefined
  // 优先按这一片自己的 summary 判；刚刷新页面还没收到 run_end 时退回全局最近一次
  const st = r.ended ? null : state.status?.last_run?.status
  if (st === 'failed') return 'exception'
  if (st === 'success') return 'success'
  return undefined
})

async function rescan() {
  rescanLoading.value = true
  try {
    const r = await api.rescanLibrary()
    ElMessage.success(r?.detail || '已触发本地库扫描')
    setTimeout(() => api.getLibrary().then((d) => (state.library = d)), 2500)
  } catch (e) {
    api.toastError(e, '触发扫描失败')
  } finally {
    rescanLoading.value = false
  }
}

async function checkAll() {
  checkingAll.value = true
  try {
    const r = await api.checkAllSources()
    ElMessage.success(r?.detail || '已在后台检测全部音源')
    // 后端在后台线程逐个探活，稍后回读一次健康表
    setTimeout(async () => {
      try {
        state.status = await api.getStatus()
      } catch {
        /* 忽略轮询失败 */
      }
    }, 3000)
  } catch (e) {
    api.toastError(e, '检测失败')
  } finally {
    checkingAll.value = false
  }
}

let timer = null
onMounted(async () => {
  await refreshDashboard()
  clock.value = Date.now()
  // 有任务在跑或在排队时定时刷新汇总数据；顺带把队列里的等待时长走秒
  timer = setInterval(() => {
    clock.value = Date.now()
    if (anyRunning.value || state.queue.depth) refreshDashboard()
  }, 8000)
})
onUnmounted(() => clearInterval(timer))

// 两个 tab 的日志都要跟着新消息滚到底
function scrollBox(el) {
  if (el) el.scrollTop = el.scrollHeight
}
watch(
  () => [live.value.tracks.length, liveTab.value],
  async () => {
    await nextTick()
    scrollBox(liveTab.value === 'tracks' ? trackBox.value : progBox.value)
  },
)
</script>

<style scoped>
.guide-card {
  border: 1px solid #b3d8ff;
  background: #ecf5ff;
}
.guide-inner {
  display: flex;
  align-items: center;
  gap: 16px;
}
.guide-text {
  flex: 1;
}
.guide-title {
  font-size: 16px;
  font-weight: 600;
  margin-bottom: 4px;
}
.guide-sub {
  color: #5b7089;
  font-size: 13px;
  line-height: 1.6;
}
.card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.card-title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-weight: 600;
  font-size: 14px;
}
.queue-box {
  font-size: 13px;
  line-height: 1.8;
}
.queue-head {
  display: flex;
  justify-content: space-between;
  margin-bottom: 4px;
}
.queue-box .run-row {
  cursor: default;
}
.multi-run {
  display: flex;
  flex-direction: column;
  gap: 4px;
  max-height: 168px;
  overflow-y: auto;
}
.run-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 5px 8px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 4px;
  font-size: 13px;
  cursor: pointer;
}
.run-row:hover {
  background: var(--el-fill-color-light);
}
.run-row.on {
  border-color: var(--el-color-primary);
  background: var(--el-color-primary-light-9);
}
.live-head {
  margin-bottom: 8px;
  font-size: 13px;
}
.live-counts {
  margin-top: 10px;
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.live-tabs {
  margin-top: 8px;
}
.tr-title {
  flex: none;
  max-width: 240px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.tr-artist {
  flex: none;
  max-width: 140px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.tr-src {
  flex: none;
}
.tr-err {
  color: #f56c6c;
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.run-line {
  font-size: 13px;
  margin-bottom: 10px;
  color: #606266;
}
.error-box {
  background: #fef0f0;
  border: 1px solid #fde2e2;
  border-radius: 6px;
  padding: 10px 12px;
}
.error-title {
  color: #f56c6c;
  font-weight: 600;
  display: flex;
  align-items: center;
  gap: 4px;
  margin-bottom: 6px;
}
.error-box ul {
  margin: 0;
  padding-left: 18px;
  color: #c45656;
  font-size: 12px;
  line-height: 1.8;
  max-height: 160px;
  overflow-y: auto;
}
.health-legend {
  margin-top: 10px;
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
}
.success {
  color: #67c23a;
}
.primary {
  color: #409eff;
}
.err-text {
  color: #f56c6c;
  font-size: 12px;
}
.err-ellipsis {
  display: block;
  max-width: 220px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
</style>