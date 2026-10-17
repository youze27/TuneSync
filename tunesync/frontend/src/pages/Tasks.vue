<template>
  <div class="page" v-loading="loading">
    <div class="page-header">
      <div>
        <h2 class="page-title">推送计划</h2>
        <div class="page-subtitle">
          每条计划自带一套推送偏好：推哪些场景、走什么来源、按什么频率执行
        </div>
      </div>
      <div class="toolbar">
        <el-button :icon="Refresh" @click="load">刷新</el-button>
        <el-button type="primary" :icon="Plus" @click="openCreate">新建计划</el-button>
      </div>
    </div>

    <el-alert type="info" show-icon :closable="false" class="mb-12">
      <template #title>
        新建与编辑请到<router-link to="/push-config">新建计划</router-link>，
        手动刮削入口在<router-link to="/scrape">刮削设置</router-link>，
        歌单命名与下载参数、模型与访问安全都在<router-link to="/settings">全局设置</router-link>。
      </template>
    </el-alert>

    <el-card shadow="never">
      <el-table :data="tasks" border stripe>
        <el-table-column type="expand">
          <template #default="{ row }">
            <div class="expand-box">
              <div>
                <span class="muted">分类明细：</span>{{ row.categories.map(catName).join('、') || '—' }}
              </div>
              <div>
                <span class="muted">歌单来源：</span>{{ sourceModeLabel(row.push_mode) }}
                <template v-if="needsSourcePlatforms(row.push_mode)">
                  · 来源 {{ sourcePlatformText(row.source_platforms) }}
                </template>
              </div>
              <div>
                <span class="muted">场景推送：</span>{{ sceneModeLabel(row.scene_mode) }}
                <span class="muted"> · 每歌单 {{ row.tracks_per || globalTracksPer }} 首</span>
              </div>
              <div>
                <span class="muted">自定义提示词：</span>
                <span v-if="row.custom_prompt">{{ row.custom_prompt }}</span>
                <span v-else class="muted">未设置</span>
              </div>
              <div><span class="muted">Cron：</span><span class="mono">{{ row.cron_expr }}</span></div>
              <div v-if="row.last_run">
                <span class="muted">最近运行：</span>
                运行 #{{ row.last_run.id }} · {{ fmtTime(row.last_run.started_at) }}
                <el-tag size="small" :type="runTag(row.last_run.status).type">
                  {{ runTag(row.last_run.status).label }}
                </el-tag>
              </div>
              <div v-else class="muted">还没有运行过</div>
            </div>
          </template>
        </el-table-column>

        <el-table-column prop="name" label="名称" min-width="140" show-overflow-tooltip>
          <template #default="{ row }">
            <span>{{ row.name }}</span>
            <el-tag v-if="row.running" size="small" type="primary" effect="dark" style="margin-left: 6px">
              运行中
            </el-tag>
          </template>
        </el-table-column>

        <el-table-column label="场景" min-width="200">
          <template #default="{ row }">
            <el-tooltip :content="row.categories.map(catName).join('、')" placement="top">
              <span>{{ summarizeCategories(row) }}</span>
            </el-tooltip>
          </template>
        </el-table-column>

        <el-table-column label="每场景数量" width="100" align="center">
          <template #default="{ row }">{{ row.count_per }}</template>
        </el-table-column>

        <el-table-column label="歌单来源" width="180">
          <template #default="{ row }">
            <el-tag size="small" effect="plain" :type="modeTagType(row.push_mode)">
              {{ sourceModeLabel(row.push_mode) }}
            </el-tag>
          </template>
        </el-table-column>

        <el-table-column label="频率" width="150">
          <template #default="{ row }">
            <span class="mono">{{ row.cron_expr }}</span>
            <div class="muted" style="font-size: 12px">{{ describeCron(row.cron_expr) }}</div>
          </template>
        </el-table-column>

        <el-table-column label="启用" width="80" align="center">
          <template #default="{ row }">
            <el-switch
              :model-value="row.enabled"
              :loading="togglingId === row.id"
              @change="(v) => toggleEnabled(row, v)"
            />
          </template>
        </el-table-column>

        <el-table-column label="最近运行" width="120">
          <template #default="{ row }">{{ fmtAgo(row.last_run?.finished_at || row.last_run?.started_at) }}</template>
        </el-table-column>

        <el-table-column label="下次运行" width="130">
          <template #default="{ row }">
            <span v-if="row.enabled && row.next_run">{{ fmtTime(row.next_run) }}</span>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>

        <el-table-column label="操作" width="200" fixed="right">
          <template #default="{ row }">
            <el-button size="small" type="primary" text :loading="runningId === row.id" @click="runNow(row)">
              立即执行
            </el-button>
            <el-button size="small" text @click="openEdit(row)">编辑</el-button>
            <el-button size="small" type="danger" text @click="remove(row)">删除</el-button>
          </template>
        </el-table-column>

        <template #empty>
          <div class="empty-hint">还没有推送计划，点击右上角「新建计划」创建。</div>
        </template>
      </el-table>
    </el-card>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Refresh } from '@element-plus/icons-vue'
import * as api from '../api.js'
import {
  SCENE_MODES,
  SOURCE_PLATFORMS,
  boot,
  catName,
  ensureSettings,
  loadTasks,
  needsSourcePlatforms,
  normaliseSourcePlatforms,
  sourceModeLabel,
  state,
  weekdayLabels,
} from '../store.js'
import { fmtAgo, fmtTime, runStatusTag } from '../utils.js'

const router = useRouter()
const loading = ref(false)
const runningId = ref(null)
const togglingId = ref(null)

const tasks = computed(() => state.tasks)

/** 列表页「每歌单 N 首」在计划没单独设时显示全局值，与推送配置页的「跟随全局」一致。 */
const globalTracksPer = computed(() => Number(state.settings?.llm?.tracks_per_playlist) || 30)

const sceneModeLabel = (mode) => SCENE_MODES.find((m) => m.key === mode)?.label || '单独推送'

/** 与推送配置页同一套配色，便于一眼分辨任务走的是哪条路。 */
const modeTagType = (mode) =>
  ({ llm: 'primary', source_llm: 'success', source_raw: 'warning' })[mode] || 'info'

const sourcePlatformText = (list) => {
  const keys = normaliseSourcePlatforms(list)
  if (keys.includes('all')) return '全平台聚合'
  return keys.map((k) => SOURCE_PLATFORMS.find((s) => s.key === k)?.name || k).join('、')
}

const runTag = (s) => runStatusTag(s)

function summarizeCategories(row) {
  const names = row.category_names || row.categories.map(catName)
  if (!names.length) return '—'
  if (names.length <= 3) return names.join('、')
  return `${names.slice(0, 3).join('、')} 等 ${names.length} 项`
}

/** 把 cron 翻成人话；识别不出就原样返回。星期名沿用后端公布的编号约定。 */
function describeCron(expr) {
  const p = String(expr || '').trim().split(/\s+/)
  if (p.length !== 5) return '自定义'
  const [min, hour, dom, mon, dow] = p
  const WD = weekdayLabels.value
  const hm = `${hour.padStart(2, '0')}:${String(min).padStart(2, '0')}`
  if (mon === '*' && dom === '*' && dow === '*') return `每天 ${hm}`
  const step = /^\*\/(\d+)$/.exec(dom)
  if (mon === '*' && step && dow === '*') return `每 ${step[1]} 天 ${hm}`
  if (mon === '*' && /^\d+$/.test(dom) && dow === '*') return `每月 ${dom} 日 ${hm}`
  if (mon === '*' && dom === '*' && /^\d$/.test(dow)) return `每${WD[Number(dow)] ?? dow} ${hm}`
  return '自定义'
}

async function load() {
  loading.value = true
  try {
    await loadTasks()
  } catch (e) {
    api.toastError(e, '加载推送计划失败')
  } finally {
    loading.value = false
  }
}

function openCreate() {
  router.push('/push-config')
}

function openEdit(row) {
  router.push({ path: '/push-config', query: { id: String(row.id) } })
}

async function toggleEnabled(row, val) {
  togglingId.value = row.id
  try {
    await api.updateTask(row.id, {
      name: row.name,
      platforms: [],
      categories: row.categories,
      custom_prompt: row.custom_prompt || '',
      count_per: row.count_per,
      cron_expr: row.cron_expr,
      enabled: val,
      // 开关只改 enabled，但后端是整行覆盖，这些字段必须原样带回
      push_mode: row.push_mode,
      source_platforms: normaliseSourcePlatforms(row.source_platforms),
      scene_mode: row.scene_mode || 'separate',
      tracks_per: row.tracks_per || 0,
    })
    row.enabled = val
    ElMessage.success(val ? '计划已启用' : '计划已停用')
  } catch (e) {
    api.toastError(e, '更新失败')
  } finally {
    togglingId.value = null
    load()
  }
}

async function runNow(row) {
  runningId.value = row.id
  try {
    const r = await api.runTask(row.id)
    ElMessage.success(r?.detail || '已触发执行')
    await load()
    router.push('/')
  } catch (e) {
    // 409 = 该任务正在运行中
    const msg = api.errText(e, '触发失败')
    if (e?.response?.status === 409) {
      ElMessage.warning(msg || '该计划正在运行中')
    } else {
      api.toastError(e, '触发失败')
    }
  } finally {
    runningId.value = null
  }
}

async function remove(row) {
  try {
    await ElMessageBox.confirm(
      `确定删除计划「${row.name}」吗？该操作不可撤销，历史运行记录会保留。`,
      '删除确认',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消', confirmButtonClass: 'el-button--danger' },
    )
  } catch {
    return
  }
  try {
    await api.deleteTask(row.id)
    ElMessage.success('计划已删除')
    await load()
  } catch (e) {
    api.toastError(e, '删除失败')
  }
}

onMounted(async () => {
  if (!state.booted) await boot()
  // 列表要显示「每歌单 N 首」的全局值与星期名，都来自 settings；已有缓存时不会重复请求
  ensureSettings().catch(() => {})
  await load()
})
</script>

<style scoped>
.expand-box {
  padding: 8px 16px 12px 48px;
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 13px;
  color: #606266;
}
</style>
