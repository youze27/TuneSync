<template>
  <div class="page">
    <div class="page-header">
      <div>
        <h2 class="page-title">本地乐库</h2>
        <div class="page-subtitle">
          按关键词 / 歌手 / 流派 / 风格 / 年份 / 格式筛选本地歌曲，勾选后直接建成歌单推送到推送平台
        </div>
      </div>
      <div class="toolbar">
        <el-button :icon="Refresh" :loading="loading" @click="reload">刷新</el-button>
        <el-button :icon="FolderOpened" @click="rescan">重新扫描音乐库</el-button>
        <el-button :icon="Delete" type="danger" plain @click="openDedupe">重复歌曲去重</el-button>
      </div>
    </div>

    <el-card shadow="never" class="mb-12">
      <div class="filters">
        <el-input
          v-model="query.q"
          placeholder="搜索歌名 / 歌手 / 路径"
          clearable
          style="width: 220px"
          @keyup.enter="doSearch"
          @clear="doSearch"
        >
          <template #append>
            <el-button :icon="Search" @click="doSearch" />
          </template>
        </el-input>
        <el-select v-model="query.artist" placeholder="全部歌手" clearable filterable style="width: 150px" @change="doSearch">
          <el-option v-for="a in facets.artists" :key="a.name" :label="`${a.name}（${a.count}）`" :value="a.name" />
        </el-select>
        <el-select v-model="query.genre" placeholder="全部流派" clearable filterable style="width: 150px" @change="doSearch">
          <el-option v-for="g in facets.genres" :key="g.name" :label="`${g.name}（${g.count}）`" :value="g.name" />
        </el-select>
        <el-select v-model="query.style" placeholder="全部风格" clearable filterable style="width: 150px" @change="doSearch">
          <el-option v-for="s in facets.styles" :key="s.name" :label="`${s.name}（${s.count}）`" :value="s.name" />
        </el-select>
        <el-select v-model="query.year" placeholder="全部年份" clearable style="width: 116px" @change="doSearch">
          <el-option v-for="y in facets.years" :key="y.name" :label="`${y.name}（${y.count}）`" :value="String(y.name)" />
        </el-select>
        <el-select v-model="query.fmt" placeholder="全部格式" clearable style="width: 116px" @change="doSearch">
          <el-option v-for="f in facets.formats" :key="f.name" :label="`${f.name}（${f.count}）`" :value="f.name" />
        </el-select>
        <el-button text :icon="RefreshLeft" @click="resetFilters">重置筛选</el-button>
        <el-button text :icon="Refresh" :loading="facetsLoading" @click="reloadFacets">刷新加载</el-button>
        <span class="muted">
          共 {{ total }} 首 · 已选 {{ selection.length }} 首
          <template v-if="facets.cached"> · 选项走缓存</template>
        </span>
        <el-tag v-if="facets.total && facets.scraped < facets.total" type="warning" effect="plain" size="small">
          {{ facets.total - facets.scraped }} 首还没有流派/风格数据，去「刮削设置」补刮
        </el-tag>
      </div>
    </el-card>

    <el-card shadow="never">
      <el-table
        ref="tableRef"
        v-loading="loading"
        :data="items"
        size="small"
        border
        stripe
        height="calc(100vh - 340px)"
        @selection-change="(rows) => (selection = rows)"
      >
        <el-table-column type="selection" width="46" reserve-selection />
        <el-table-column prop="title" label="歌名" min-width="180" show-overflow-tooltip />
        <el-table-column prop="artist" label="歌手" min-width="120" show-overflow-tooltip />
        <el-table-column label="流派 / 风格" min-width="150" show-overflow-tooltip>
          <template #default="{ row }">
            <span v-if="row.genre || row.style">{{ row.genre || '—' }}<span v-if="row.style" class="muted"> / {{ row.style }}</span></span>
            <span v-else class="muted">未刮削</span>
          </template>
        </el-table-column>
        <el-table-column prop="album" label="专辑" min-width="140" show-overflow-tooltip />
        <el-table-column label="年份" width="80" align="center">
          <template #default="{ row }">{{ row.year || '—' }}</template>
        </el-table-column>
        <el-table-column label="格式" width="80" align="center">
          <template #default="{ row }">
            <el-tag size="small" effect="plain">{{ (row.format || '').toUpperCase() || '未知' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="大小" width="90" align="right">
          <template #default="{ row }">{{ fmtSize(row.size) }}</template>
        </el-table-column>
        <el-table-column prop="path" label="路径" min-width="220" show-overflow-tooltip class-name="mono" />
        <template #empty>
          <span class="muted">
            {{ facets.total ? '没有匹配的歌曲，换个筛选条件试试' : '本地索引还是空的，先点「重新扫描音乐库」' }}
          </span>
        </template>
      </el-table>

      <div class="pager">
        <el-pagination
          layout="total, prev, pager, next"
          :total="total"
          :page-size="pageSize"
          :current-page="page"
          @current-change="goPage"
        />
      </div>
    </el-card>

    <div class="create-bar">
      <el-input v-model="playlistName" placeholder="给新歌单起个名字" style="width: 240px" maxlength="64" />
      <el-button type="primary" :loading="creating" :disabled="!selection.length" @click="createPlaylist">
        用选中的 {{ selection.length }} 首建歌单
      </el-button>
      <el-button text :disabled="!selection.length" @click="clearSelection">清空选择</el-button>
      <span v-if="lastResult" class="muted">{{ lastResult }}</span>
    </div>

    <el-dialog v-model="dedupeVisible" title="重复歌曲去重" width="880px" top="6vh">
      <el-alert type="warning" :closable="false" show-icon class="mb-12">
        <template #title>
          去重会<strong>直接删除磁盘文件</strong>，不可撤销。建议先「预演」看一遍，确认无误再执行。
        </template>
      </el-alert>

      <div class="filters mb-12">
        <el-radio-group v-model="dedupeScope" size="small" @change="loadDuplicates">
          <el-radio-button value="exact">完全相同（格式+大小一致）</el-radio-button>
          <el-radio-button value="song">同名不同版本（仅查看）</el-radio-button>
        </el-radio-group>
        <el-button size="small" :icon="Refresh" :loading="dedupeLoading" @click="loadDuplicates">重新检测</el-button>
        <span class="muted">
          {{ dup.groups_count || 0 }} 组 · 冗余 {{ dup.redundant || 0 }} 份 · 可释放
          {{ fmtSize(dup.freed_bytes || 0) }}
        </span>
      </div>

      <el-alert v-if="dedupeProgress.total" type="info" :closable="false" class="mb-12">
        <template #title>
          正在{{ dedupeProgress.dry_run ? '预演' : '删除' }}：{{ dedupeProgress.done }} /
          {{ dedupeProgress.total }} 份
        </template>
      </el-alert>
      <el-progress
        v-if="dedupeBusy && dedupeProgress.total"
        :percentage="pct"
        :stroke-width="10"
        class="mb-12"
      />

      <el-table :data="shownGroups" size="small" border max-height="380" empty-text="没有发现重复歌曲">
        <el-table-column label="歌曲" min-width="160" show-overflow-tooltip>
          <template #default="{ row }">{{ row.title }}<span v-if="row.artist" class="muted"> · {{ row.artist }}</span></template>
        </el-table-column>
        <el-table-column label="格式" width="80" align="center">
          <template #default="{ row }">{{ (row.format || '').toUpperCase() || '未知' }}</template>
        </el-table-column>
        <el-table-column label="每份大小" width="100" align="right">
          <template #default="{ row }">{{ fmtSize(row.size) }}</template>
        </el-table-column>
        <el-table-column prop="keep.path" label="保留这一份" min-width="240" show-overflow-tooltip class-name="mono" />
        <el-table-column label="将删除" min-width="200">
          <template #default="{ row }">
            <div v-for="d in row.duplicates" :key="d.id" class="dup-line">
              <el-tag
                size="small"
                :type="spared.has(d.path) ? 'info' : 'danger'"
                :effect="spared.has(d.path) ? 'plain' : 'light'"
                closable
                @close="uncheck(d.path)"
              >
                {{ d.path.split(/[\\/]/).pop() }}{{ spared.has(d.path) ? '（已豁免）' : '' }}
              </el-tag>
            </div>
          </template>
        </el-table-column>
      </el-table>
      <div v-if="hiddenGroups > 0" class="muted mb-12">
        仅显示前 {{ GROUP_PAGE }} 组，另有 {{ hiddenGroups }} 组未展示；删除时仍会处理全部选中项。
      </div>

      <div class="dup-preview">
        预演结果：{{ preview.removed || 0 }} 份可删 · 释放 {{ fmtSize(preview.freed_bytes || 0) }}
        <span v-if="preview.failed?.length" class="err">，{{ preview.failed.length }} 份被跳过</span>
      </div>
      <el-collapse v-if="preview.files?.length" class="mb-12">
        <el-collapse-item title="查看预演明细清单">
          <div class="dup-list">
            <div v-for="f in preview.files" :key="f.path" class="mono dup-list-line">
              <span class="muted">{{ fmtSize(f.size) }}</span> {{ f.path }}
            </div>
          </div>
        </el-collapse-item>
      </el-collapse>

      <template #footer>
        <el-button @click="dedupeVisible = false">关闭</el-button>
        <el-button :loading="dedupeBusy" @click="doDedupe(true)">预演（不删除）</el-button>
        <el-button
          type="danger"
          :loading="dedupeBusy"
          :disabled="dup.scope !== 'exact'"
          @click="doDedupe(false)"
        >
          确认删除
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Delete, FolderOpened, Refresh, RefreshLeft, Search } from '@element-plus/icons-vue'
import * as api from '../api.js'

const tableRef = ref()
const items = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = 50
const loading = ref(false)
const creating = ref(false)
const selection = ref([])
const playlistName = ref('')
const lastResult = ref('')
const facets = ref({ total: 0, scraped: 0, cached: false, artists: [], genres: [], styles: [], years: [], formats: [] })
const facetsLoading = ref(false)
const query = reactive({ q: '', artist: '', genre: '', style: '', year: '', fmt: '' })

// 去重弹窗
const dedupeVisible = ref(false)
const dedupeScope = ref('exact')
const dedupeLoading = ref(false)
const dedupeBusy = ref(false)
const dup = ref({ scope: 'exact', groups: [], groups_count: 0, redundant: 0, freed_bytes: 0 })
const preview = ref({ removed: 0, freed_bytes: 0, failed: [] })
/** 手动标记「这一份别删」的路径，用于保留拿不准的副本 */
const spared = ref(new Set())
const dedupeProgress = ref({ running: false, dry_run: true, done: 0, total: 0 })
/** 同名不同版本动辄几千组，全量塞进 el-table 会把界面卡死，只渲染前 N 组 */
const GROUP_PAGE = 300
const shownGroups = computed(() => (dup.value.groups || []).slice(0, GROUP_PAGE))
const hiddenGroups = computed(() => Math.max(0, (dup.value.groups || []).length - GROUP_PAGE))
const pct = computed(() => {
  const { done, total } = dedupeProgress.value
  return total ? Math.min(100, Math.round((done / total) * 100)) : 0
})

function openDedupe() {
  dedupeVisible.value = true
  spared.value = new Set()
  preview.value = { removed: 0, freed_bytes: 0, failed: [] }
  loadDuplicates()
}

async function loadDuplicates() {
  dedupeLoading.value = true
  try {
    dup.value = await api.getDuplicates(dedupeScope.value)
    spared.value = new Set()
    preview.value = { removed: 0, freed_bytes: 0, failed: [] }
  } catch (e) {
    api.toastError(e, '检测重复歌曲失败')
  } finally {
    dedupeLoading.value = false
  }
}

function uncheck(path) {
  const s = new Set(spared.value)
  s.has(path) ? s.delete(path) : s.add(path)
  spared.value = s
}

/** 轮询后端进度，WS 断了也能拿到准数 */
async function pollDedupe() {
  while (dedupeProgress.value.running) {
    try {
      dedupeProgress.value = await api.getDedupeStatus()
    } catch {
      /* 网络抖动，下一轮再试 */
    }
    if (!dedupeProgress.value.running) break
    await new Promise((r) => setTimeout(r, 1200))
  }
}

async function doDedupe(dryRun) {
  if (!dryRun) {
    try {
      await ElMessageBox.confirm(
        `将永久删除磁盘上的重复文件，且不可撤销。建议先点「预演」确认清单。`,
        '确认执行去重？',
        { type: 'warning', confirmButtonText: '我已确认，删除', cancelButtonText: '再想想' }
      )
    } catch {
      return
    }
  }
  dedupeBusy.value = true
  try {
    const skip = spared.value
    const paths = (dup.value.groups || [])
      .flatMap((g) => g.duplicates.map((d) => d.path))
      .filter((p) => !skip.has(p))
    if (dryRun) {
      const d = await api.runDedupe({ paths, dry_run: true })
      preview.value = d
      ElMessage.info(`预演完成：${d.removed} 份将被删除，释放 ${fmtSize(d.freed_bytes)}`)
      return
    }
    // 删除走后台线程：NAS 上删几千个文件要几分钟，同步等 HTTP 早超时报错了
    await api.runDedupe({ paths, dry_run: false, async_run: true })
    dedupeProgress.value = await api.getDedupeStatus()
    await pollDedupe()
    const st = dedupeProgress.value
    ElMessage.success(`已删除 ${st.removed} 份重复文件，释放 ${fmtSize(st.freed_bytes)}`)
    if (st.failed) ElMessage.warning(`${st.failed} 份删除失败，详见日志`)
    dedupeVisible.value = false
    await Promise.all([loadFacets(), reload()])
  } catch (e) {
    api.toastError(e, dryRun ? '预演失败' : '去重失败')
  } finally {
    dedupeBusy.value = false
  }
}

async function loadFacets(refresh = false) {
  try {
    facets.value = await api.getLocalFacets(refresh)
  } catch {
    /* 下拉选项拉不到不影响列表 */
  }
}

/** 「刷新加载」：强制绕过服务端缓存重建下拉选项，并重刷列表 */
async function reloadFacets() {
  facetsLoading.value = true
  try {
    await loadFacets(true)
    await reload()
    ElMessage.success('筛选选项已重新加载')
  } finally {
    facetsLoading.value = false
  }
}

async function reload() {
  loading.value = true
  try {
    const d = await api.getLocalFiles({ ...query, page: page.value, limit: pageSize })
    items.value = d.items
    total.value = d.total
  } catch (e) {
    api.toastError(e, '读取本地乐库失败')
  } finally {
    loading.value = false
  }
}

function doSearch() {
  page.value = 1
  reload()
}

function resetFilters() {
  Object.assign(query, { q: '', artist: '', genre: '', style: '', year: '', fmt: '' })
  doSearch()
}

function goPage(p) {
  page.value = p
  reload()
}

async function rescan() {
  await api.rescanLibrary().catch((e) => api.toastError(e, '触发扫描失败'))
  ElMessage.success('已触发后台扫描，完成后点「刷新」查看')
  setTimeout(loadFacets, 3000)
}

function clearSelection() {
  tableRef.value?.clearSelection()
  selection.value = []
}

function fmtSize(n) {
  const v = Number(n) || 0
  if (v < 1024) return `${v} B`
  if (v < 1024 * 1024) return `${(v / 1024).toFixed(0)} KB`
  return `${(v / 1024 / 1024).toFixed(1)} MB`
}

async function createPlaylist() {
  const name = playlistName.value.trim()
  if (!name) {
    ElMessage.warning('先给歌单起个名字')
    return
  }
  creating.value = true
  lastResult.value = ''
  try {
    const d = await api.createLocalPlaylist({ name, paths: selection.value.map((r) => r.path) })
    lastResult.value = d.results.map((r) => `${r.target}：${r.detail}`).join('　')
    ElMessage.success(`歌单「${name}」已写入 ${d.tracks} 首`)
    playlistName.value = ''
    clearSelection()
  } catch (e) {
    api.toastError(e, '创建歌单失败')
  } finally {
    creating.value = false
  }
}

const scanned = computed(() => facets.value.total > 0)

onMounted(async () => {
  await loadFacets()
  await reload()
  if (!scanned.value) ElMessage.info('本地索引为空，可点「重新扫描音乐库」建立索引')
})
</script>

<style scoped>
.filters {
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
}
.pager {
  display: flex;
  justify-content: flex-end;
  padding-top: 10px;
}
.create-bar {
  position: sticky;
  bottom: 0;
  margin-top: 12px;
  padding: 10px 12px;
  background: #fff;
  border: 1px solid #e6e8eb;
  border-radius: 8px;
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
}
.dup-line {
  margin-bottom: 4px;
}
.dup-preview {
  margin-top: 10px;
  font-size: 13px;
  color: #606266;
}
.dup-preview .err {
  color: #f56c6c;
}
.dup-list {
  max-height: 240px;
  overflow: auto;
  font-size: 12px;
}
.dup-list-line {
  padding: 2px 0;
  border-bottom: 1px solid #ebeef5;
  word-break: break-all;
}
</style>
