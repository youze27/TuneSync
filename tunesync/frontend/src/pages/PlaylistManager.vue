<template>
  <div class="page">
    <div class="page-header">
      <div>
        <h2 class="page-title">歌单管理</h2>
        <div class="page-subtitle">查看飞牛与 Navidrome 里已有的歌单，删除不需要的，并按生成时间定时清理老歌单</div>
      </div>
      <div class="toolbar">
        <el-button size="small" :icon="Refresh" :loading="loading" @click="loadAll">刷新</el-button>
        <el-button size="small" type="primary" :icon="Delete" @click="openCleanup">定时清理</el-button>
      </div>
    </div>

    <el-card shadow="never" class="mb-12">
      <div class="cmp">
        <el-radio-group v-model="target" size="small" @change="loadList">
          <el-radio-button v-for="t in targets" :key="t.key" :value="t.key" :disabled="!t.available">
            {{ t.name }}
            <el-tag v-if="!t.available" size="small" type="info" effect="plain" round>未配置</el-tag>
          </el-radio-button>
        </el-radio-group>
        <div class="cmp-tools">
          <el-input v-model="keyword" size="small" placeholder="按歌单名筛选" clearable class="cmp-search" />
          <el-select v-model="sortBy" size="small" class="cmp-sort">
            <el-option label="按生成时间 · 新→旧" value="new" />
            <el-option label="按生成时间 · 旧→新" value="old" />
            <el-option label="按曲目数 · 多→少" value="tracks" />
          </el-select>
        </div>
      </div>
      <div class="cmp-stats">
        <span>共 <b>{{ filtered.length }}</b> 张歌单</span>
        <span>合计 <b>{{ totalTracks }}</b> 首</span>
        <span v-if="currentTarget">· 来源：{{ currentTarget.name }}</span>
        <span v-if="error" class="tr-err">{{ error }}</span>
      </div>
    </el-card>

    <el-card shadow="never" v-loading="loading">
      <el-table :data="paged" size="small" style="width: 100%" empty-text="该目标下没有歌单">
        <el-table-column type="selection" width="42" />
        <el-table-column prop="name" label="歌单" min-width="240">
          <template #default="{ row }">
            <span class="break-all">{{ row.name || '(未命名)' }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="tracks" label="曲目" width="80" align="right" sortable />
        <el-table-column label="生成时间" width="180">
          <template #default="{ row }">
            <span v-if="row.created">{{ fmtTime(row.created) }}</span>
            <el-tag v-else size="small" type="info" effect="plain">目标未记录</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="时长" width="100">
          <template #default="{ row }">{{ row.duration ? fmtDuration(row.duration) : '—' }}</template>
        </el-table-column>
        <el-table-column prop="id" label="ID" width="150">
          <template #default="{ row }">
            <span class="mono muted">{{ row.id }}</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="120" align="right" fixed="right">
          <template #default="{ row }">
            <el-button size="small" text type="danger" :icon="Delete" @click="remove(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>

      <div class="pager">
        <el-pagination
          v-if="filtered.length > pageSize"
          v-model:current-page="page"
          :page-size="pageSize"
          :total="filtered.length"
          layout="prev, pager, next, total"
          size="small"
          background
        />
      </div>
    </el-card>

    <!-- 定时清理：默认只预览，确认后真删 -->
    <el-dialog v-model="cleanupVisible" title="按生成时间清理老旧歌单" width="520px">
      <el-form label-width="100px" label-position="left">
        <el-form-item label="保留天数">
          <el-input-number v-model="keepDays" :min="1" :max="3650" controls-position="right" style="width: 140px" />
          <span class="muted">　生成时间早于「今天 − {{ keepDays }} 天」的歌单会被删除</span>
        </el-form-item>
        <el-form-item label="作用范围">
          <el-radio-group v-model="cleanupScope" size="small">
            <el-radio value="all">全部推送目标</el-radio>
            <el-radio value="current">仅 {{ currentTarget?.name || '当前' }}</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="定时执行">
          <el-switch v-model="autoEnabled" />
          <span class="muted">　每 {{ intervalMin }} 分钟检查一次</span>
        </el-form-item>
      </el-form>

      <el-alert
        v-if="preview"
        :type="preview.total ? 'warning' : 'success'"
        :closable="false"
        show-icon
        :title="preview.total ? `将删除 ${preview.total} 张老歌单` : '没有需要清理的老歌单'"
      >
        <div class="clean-list scroll-y">
          <div v-for="r in preview.results" :key="r.target">
            <div v-if="!r.ok" class="muted">{{ labelOf(r.target) }}：{{ r.error }}</div>
            <div v-else-if="!r.removed.length" class="muted">{{ labelOf(r.target) }}：无</div>
            <div v-else>
              <b>{{ labelOf(r.target) }}</b>（扫描 {{ r.scanned }} 张）
              <div v-for="p in r.removed" :key="p.id" class="clean-item">
                · {{ p.name }} <span class="muted">{{ fmtTime(p.created) }}</span>
              </div>
            </div>
          </div>
        </div>
      </el-alert>

      <template #footer>
        <el-button size="small" @click="cleanupVisible = false">关闭</el-button>
        <el-button size="small" :loading="busy" @click="runCleanup(true)">重新预览</el-button>
        <el-button size="small" type="primary" :loading="savingCleanup" @click="saveCleanupSettings()">
          保存设置
        </el-button>
        <el-button size="small" type="danger" :disabled="!preview?.total" :loading="busy" @click="runCleanup(false)">
          确认删除
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Delete, Refresh } from '@element-plus/icons-vue'
import { cleanupPlaylists, deleteTargetPlaylist, getPlaylistTargets, getSettings, getTargetPlaylists, putSettings, toastError } from '../api.js'
import { invalidateSettings } from '../store.js'

const targets = ref([])
const target = ref('')
const lists = reactive({})          // { [target]: playlists[] }
const keyword = ref('')
const sortBy = ref('new')
const loading = ref(false)
const error = ref('')
const page = ref(1)
const pageSize = 50

const currentTarget = computed(() => targets.value.find((t) => t.key === target.value))
const raw = computed(() => lists[target.value] || [])
const filtered = computed(() => {
  const kw = keyword.value.trim().toLowerCase()
  let arr = kw ? raw.value.filter((p) => (p.name || '').toLowerCase().includes(kw)) : [...raw.value]
  if (sortBy.value === 'new') arr.sort((a, b) => ts(b) - ts(a))
  else if (sortBy.value === 'old') arr.sort((a, b) => ts(a) - ts(b))
  else arr.sort((a, b) => (b.tracks || 0) - (a.tracks || 0))
  return arr
})
const paged = computed(() => filtered.value.slice((page.value - 1) * pageSize, page.value * pageSize))
const totalTracks = computed(() => filtered.value.reduce((s, p) => s + (p.tracks || 0), 0))
watch([keyword, sortBy, target], () => { page.value = 1 })

const ts = (p) => p.created || p.changed || 0

const labelOf = (key) => targets.value.find((t) => t.key === key)?.name || key

function fmtTime(sec) {
  if (!sec) return '—'
  const d = new Date(sec * 1000)
  const p = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

function fmtDuration(sec) {
  const s = Math.round(sec || 0)
  if (s < 60) return `${s} 秒`
  if (s < 3600) return `${Math.round(s / 60)} 分`
  return `${(s / 3600).toFixed(1)} 小时`
}

async function loadTargets() {
  const r = await getPlaylistTargets()
  targets.value = r.targets || []
  if (!target.value) {
    const first = targets.value.find((t) => t.available)
    target.value = first ? first.key : ''
  }
}

async function loadList() {
  if (!target.value) return
  loading.value = true
  error.value = ''
  try {
    const r = await getTargetPlaylists(target.value)
    lists[target.value] = r.playlists || []
  } catch (e) {
    error.value = toastError(e, '读取歌单列表失败')
    lists[target.value] = []
  } finally {
    loading.value = false
  }
}

async function loadAll() {
  loading.value = true
  try {
    await loadTargets()
    await Promise.all(targets.value.filter((t) => t.available).map((t) => getTargetPlaylists(t.key)
      .then((r) => { lists[t.key] = r.playlists || [] })
      .catch((e) => { lists[t.key] = []; error.value = toastError(e, `${t.name} 读取失败`) })))
  } finally {
    loading.value = false
  }
}

async function remove(row) {
  try {
    await ElMessageBox.confirm(
      `确定删除「${row.name}」？该操作不可撤销（只删歌单，不动音乐文件）。`,
      '删除歌单',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消', confirmButtonClass: 'el-button--danger' },
    )
  } catch {
    return
  }
  try {
    await deleteTargetPlaylist(target.value, row.id)
    ElMessage.success(`已删除「${row.name}」`)
    await loadList()
  } catch (e) {
    toastError(e, '删除失败')
  }
}

// ─────────── 定时清理
const cleanupVisible = ref(false)
const keepDays = ref(180)
const cleanupScope = ref('all')
const autoEnabled = ref(false)
const intervalMin = ref(1440)
const busy = ref(false)
const savingCleanup = ref(false)
const preview = ref(null)

async function openCleanup() {
  try {
    // 直接 GET 一份最新，不走 store 的 settings 缓存（那是 boot 阶段的快照）
    const s = await getSettings()
    const c = s.playlist_cleanup || {}
    keepDays.value = c.keep_days || 180
    intervalMin.value = c.interval_min || 1440
    autoEnabled.value = c.enabled !== false
  } catch (e) {
    toastError(e, '读取清理设置失败')
  }
  preview.value = null
  cleanupVisible.value = true
  runCleanup(true)
}

/** 只保存定时清理参数，不动任何歌单。 */
async function saveCleanupSettings(showToast = true) {
  savingCleanup.value = true
  try {
    await putSettings({
      playlist_cleanup: { enabled: autoEnabled.value, keep_days: keepDays.value, interval_min: intervalMin.value },
    })
    invalidateSettings()
    if (showToast) ElMessage.success('定时清理设置已保存')
  } catch (e) {
    toastError(e, '保存定时清理设置失败')
  } finally {
    savingCleanup.value = false
  }
}

async function runCleanup(dryRun) {
  busy.value = true
  try {
    const names = cleanupScope.value === 'current' && target.value
      ? (lists[target.value] || []).map((p) => p.name)
      : null
    const r = await cleanupPlaylists({ keep_days: keepDays.value, dry_run: dryRun, names })
    preview.value = r
    if (dryRun) return

    const failed = (r.results || []).reduce((s, x) => s + (x.failed?.length || 0), 0)
    if (failed) ElMessage.warning(`已删除 ${r.total} 张，${failed} 张失败，详见下方列表`)
    else ElMessage.success(`已删除 ${r.total} 张老歌单`)
    await loadAll()
    preview.value = await cleanupPlaylists({ keep_days: keepDays.value, dry_run: true, names })
    // 顺手把当前表单里的参数落库，避免下次进来又是旧值
    await saveCleanupSettings(false)
  } catch (e) {
    toastError(e, dryRun ? '预览失败' : '清理失败')
  } finally {
    busy.value = false
  }
}

onMounted(loadAll)
</script>

<style scoped>
.cmp {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}
.cmp-tools {
  display: flex;
  gap: 8px;
}
.cmp-search {
  width: 200px;
}
.cmp-sort {
  width: 180px;
}
.cmp-stats {
  margin-top: 10px;
  font-size: 13px;
  color: #909399;
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
  align-items: center;
}
.pager {
  display: flex;
  justify-content: flex-end;
  margin-top: 10px;
}
.clean-list {
  margin-top: 8px;
  max-height: 200px;
  font-size: 13px;
  line-height: 1.7;
}
.clean-item {
  padding-left: 8px;
}
</style>
