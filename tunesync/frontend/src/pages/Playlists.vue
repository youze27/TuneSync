<template>
  <div class="page">
    <div class="page-header">
      <div>
        <h2 class="page-title">歌单广场</h2>
        <div class="page-subtitle">
          浏览平台歌单，一键下载到本地并推送到飞牛与 Navidrome
        </div>
      </div>
      <div class="toolbar sq-toolbar">
        <el-radio-group v-model="platform" @change="switchPlatform">
          <el-radio-button v-for="p in platforms" :key="p.key" :value="p.key">{{ p.name }}</el-radio-button>
        </el-radio-group>
        <el-select v-model="cat" class="sq-cat" :disabled="!canSearch" @change="reload">
          <el-option v-for="c in cats" :key="c.value" :label="c.label" :value="c.value" />
        </el-select>
        <!-- 搜索框常驻占位：以前用 v-if 整块消失，切平台时后面的「刷新」按钮会左跳 -->
        <el-input
          v-model="searchKw"
          :placeholder="canSearch ? '搜歌单：民谣、经典、怀旧' : `${platformNameOf(platform)} 不支持关键词搜索`"
          :disabled="!canSearch"
          clearable
          @keyup.enter="doSearch"
          @clear="doSearch"
        >
          <template #append>
            <el-button :icon="Search" :disabled="!canSearch" @click="doSearch" />
          </template>
        </el-input>
        <el-button :icon="Refresh" :loading="loading" @click="reload">刷新</el-button>
      </div>
    </div>

    <el-card shadow="never" class="link-card">
      <div class="link-row">
        <el-input
          v-model="linkInput"
          placeholder="粘贴歌单分享链接或纯数字 ID（网易云 / QQ / 酷狗 / 酷我 / 咪咕），回车直接预览"
          clearable
          @keyup.enter="openByLink"
        >
          <template #prepend>歌单直达</template>
        </el-input>
        <el-button type="primary" :loading="parsing" @click="openByLink">解析预览</el-button>
      </div>
    </el-card>

    <div v-loading="loading" class="grid-wrap">
      <div class="pl-grid">
        <div v-for="pl in lists" :key="pl.id" class="pl-card" @click="openDetail(pl)">
          <el-image :src="pl.cover" fit="cover" class="pl-cover" lazy>
            <template #error>
              <div class="pl-cover-fallback"><el-icon :size="28"><Headset /></el-icon></div>
            </template>
          </el-image>
          <div class="pl-name" :title="pl.name">{{ pl.name }}</div>
          <div class="pl-meta">
            <span v-if="pl.platform" class="pl-badge">{{ platformNameOf(pl.platform) }}</span>
            <span v-if="pl.count">{{ pl.count }} 首</span>
            <span v-if="pl.play" class="pl-play">{{ fmtPlay(pl.play) }} 次播放</span>
          </div>
        </div>
        <template v-if="!loading && !lists.length">
          <el-empty description="这个分类暂时没有歌单" />
        </template>
      </div>

      <div class="pager" v-if="total > pageSize">
        <el-pagination
          layout="prev, pager, next"
          :total="total"
          :page-size="pageSize"
          :current-page="page"
          @current-change="goPage"
        />
      </div>
    </div>

    <!-- 歌单详情 + 导入 -->
    <el-dialog v-model="dialogVisible" :title="detail?.name || '歌单预览'" :width="isMobile ? '92%' : '600px'" top="5vh">
      <div v-loading="detailLoading" class="detail-head" v-if="detail">
        <el-image :src="detail.cover" fit="cover" class="detail-cover">
          <template #error>
            <div class="pl-cover-fallback"><el-icon :size="28"><Headset /></el-icon></div>
          </template>
        </el-image>
        <div class="detail-info">
          <div class="detail-name">{{ detail.name }}</div>
          <div class="muted">{{ detail.tracks.length }} 首曲目 · {{ platformName }}</div>
          <div class="muted" style="margin-top: 4px; font-size: 12px">
            逐首搜索下载，本地已有的自动复用
          </div>
        </div>
      </div>

      <el-table v-if="detail" :data="detail.tracks" max-height="420" size="small" border stripe>
        <el-table-column type="index" label="#" width="48" />
        <el-table-column prop="title" label="歌名" min-width="160" show-overflow-tooltip />
        <el-table-column prop="artist" label="歌手" min-width="120" show-overflow-tooltip />
        <el-table-column prop="album" label="专辑" min-width="130" show-overflow-tooltip />
        <template #empty><span class="muted">没有曲目</span></template>
      </el-table>

      <template #footer>
        <el-button @click="dialogVisible = false">关闭</el-button>
        <el-button
          type="primary"
          :loading="importing"
          :disabled="!detail || !detail.tracks.length"
          @click="doImport"
        >
          <el-icon style="margin-right: 4px"><Download /></el-icon>
          推送歌单
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Download, Headset, Refresh, Search } from '@element-plus/icons-vue'
import * as api from '../api.js'

const router = useRouter()

/** 手机端弹窗用 92% 宽度；与 App.vue 的断点保持一致。 */
const isMobile = ref(false)
let mq = null
function applyMobile() {
  isMobile.value = !!mq?.matches
}

const platforms = ref([
  { key: 'all', name: '聚合' },
  { key: 'wy', name: '网易云' },
  { key: 'tx', name: 'QQ 音乐' },
  { key: 'kg', name: '酷狗' },
  { key: 'kw', name: '酷我' },
  { key: 'mg', name: '咪咕' },
])
const platform = ref('all')
const cats = ref([])
const cat = ref('')
const lists = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = 30
const loading = ref(false)
const parsing = ref(false)
// 歌单直达输入框的绑定变量。以前漏了声明，linkInput 一直是 undefined：
// v-model 写进了非响应式属性，Element Plus 下一次同步就把输入框清空，
// 粘贴进去的链接瞬间消失，回车也只会提示「请先粘贴歌单链接或 ID」。
const linkInput = ref('')

// 搜索式广场：kg/kw/mg/all 靠关键词搜歌单，分类下拉即预设关键词
const SEARCH_PLATFORMS = ['all', 'kg', 'kw', 'mg']
const searchKw = ref('')
const canSearch = computed(() => SEARCH_PLATFORMS.includes(platform.value))

const dialogVisible = ref(false)
const detailLoading = ref(false)
const detail = ref(null)
const importing = ref(false)

const platformName = computed(
  () => platforms.value.find((p) => p.key === (detail.value?.platform || platform.value))?.name || '',
)

function platformNameOf(key) {
  return platforms.value.find((p) => p.key === key)?.name || key
}

function fmtPlay(n) {
  const v = Number(n) || 0
  if (v >= 100000000) return `${(v / 100000000).toFixed(1)} 亿`
  if (v >= 10000) return `${Math.round(v / 10000)} 万`
  return String(v)
}

async function loadCategories() {
  cats.value = await api.getPlaylistCategories(platform.value)
  // 分类默认取第一项；QQ 的第一项是「全部」
  cat.value = cats.value[0]?.value || ''
}

async function loadSquare() {
  loading.value = true
  try {
    const d = await api.getPlaylistSquare(platform.value, cat.value, page.value, pageSize)
    lists.value = d.lists || []
    total.value = d.total || lists.value.length
    // 聚合模式：部分平台失败仍然展示成功的平台，但给出提示
    if (d.errors && Object.keys(d.errors).length) {
      ElMessage.warning(
        Object.entries(d.errors).map(([k, v]) => `${platformNameOf(k)}：${v}`).join('；'),
      )
    }
  } catch (e) {
    api.toastError(e, '拉取歌单广场失败')
    lists.value = []
  } finally {
    loading.value = false
  }
}

async function switchPlatform() {
  page.value = 1
  searchKw.value = ''
  try {
    await loadCategories()
  } catch (e) {
    api.toastError(e, '拉取分类失败')
    return
  }
  await loadSquare()
}

function reload() {
  page.value = 1
  loadSquare()
}

function doSearch() {
  cat.value = (searchKw.value || '').trim()
  page.value = 1
  loadSquare()
}

function goPage(p) {
  page.value = p
  loadSquare()
}

async function openByLink() {
  const link = (linkInput.value || '').trim()
  if (!link) {
    ElMessage.warning('请先粘贴歌单链接或 ID')
    return
  }
  parsing.value = true
  try {
    const d = await api.getPlaylistDetail(platform.value, link)
    showDetail(d)
  } catch (e) {
    api.toastError(e, '解析歌单失败')
  } finally {
    parsing.value = false
  }
}

function showDetail(d) {
  detail.value = d
  dialogVisible.value = true
}

async function openDetail(pl) {
  dialogVisible.value = true
  detailLoading.value = true
  detail.value = null
  try {
    const d = await api.getPlaylistDetail(pl.platform || platform.value, pl.id)
    detail.value = d
  } catch (e) {
    api.toastError(e, '拉取歌单详情失败')
    dialogVisible.value = false
  } finally {
    detailLoading.value = false
  }
}

async function doImport() {
  if (!detail.value) return
  importing.value = true
  try {
    const body = { platform: detail.value.platform, link: detail.value.id }
    await api.importPlaylist(body.platform, body.link)
    ElMessage.success('已开始导入：下载完成后自动推送到飞牛与 Navidrome，可在运行历史里看进度')
    dialogVisible.value = false
    router.push('/runs')
  } catch (e) {
    api.toastError(e, '导入失败')
  } finally {
    importing.value = false
  }
}

onMounted(async () => {
  mq = window.matchMedia('(max-width: 768px)')
  applyMobile()
  mq.addEventListener('change', applyMobile)
  try {
    const ps = await api.getPlaylistPlatforms()
    if (Array.isArray(ps) && ps.length) platforms.value = ps
  } catch { /* 静态兜底 */ }
  // 默认进聚合 tab（与后端平台列表一致时才生效）
  if (platforms.value.some((p) => p.key === 'all')) platform.value = 'all'
  await switchPlatform()
})

onUnmounted(() => {
  mq?.removeEventListener('change', applyMobile)
  mq = null
})
</script>

<style scoped>
/* 顶部工具栏：宽度全部写死，切平台时后面的控件不发生位移 */
.sq-toolbar {
  flex-wrap: nowrap;
}
.sq-cat {
  width: 150px;
  flex: none;
}
.sq-toolbar > .el-input {
  width: 230px;
  flex: none;
}
.link-card {
  margin-bottom: 14px;
}
.link-row {
  display: flex;
  gap: 10px;
}
.grid-wrap {
  min-height: 300px;
}
.pl-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
  gap: 16px;
}
.pl-card {
  cursor: pointer;
  border-radius: 8px;
  background: #fff;
  border: 1px solid #eceef1;
  padding: 10px;
  transition: box-shadow 0.15s, transform 0.15s;
}
.pl-card:hover {
  box-shadow: 0 4px 14px rgba(31, 45, 61, 0.12);
  transform: translateY(-2px);
}
.pl-cover {
  width: 100%;
  aspect-ratio: 1;
  border-radius: 8px;
  display: block;
  background: #f2f4f7;
}
.pl-cover-fallback {
  width: 100%;
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #c0c4cc;
}
.pl-name {
  margin-top: 8px;
  font-size: 13px;
  line-height: 1.35;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  min-height: 35px;
}
.pl-meta {
  margin-top: 4px;
  display: flex;
  justify-content: space-between;
  color: #8a919e;
  font-size: 12px;
}
.pl-badge {
  background: #f0f2f5;
  color: #606266;
  border-radius: 4px;
  padding: 0 6px;
  font-size: 11px;
  line-height: 18px;
  white-space: nowrap;
}
.pl-play {
  white-space: nowrap;
}
.pager {
  margin-top: 16px;
  display: flex;
  justify-content: center;
}
.detail-head {
  display: flex;
  gap: 14px;
  margin-bottom: 12px;
}
.detail-cover {
  width: 96px;
  height: 96px;
  border-radius: 8px;
  flex: none;
  background: #f2f4f7;
}
.detail-name {
  font-size: 16px;
  font-weight: 600;
  margin-bottom: 4px;
}

/* ───────────────────────────── 手机端 */
@media (max-width: 768px) {
  /* 工具栏一行放不下就横向滚，不要换行挤压 */
  .sq-toolbar {
    overflow-x: auto;
    flex-wrap: nowrap;
    width: 100%;
    padding-bottom: 4px;
  }
  /* 平台切换 tabs 也别被压扁 */
  .sq-toolbar :deep(.el-radio-group) {
    flex: none;
  }
  .pl-grid {
    grid-template-columns: repeat(auto-fill, minmax(110px, 1fr));
    gap: 10px;
  }
  .pl-card {
    padding: 8px;
  }
  .link-row {
    flex-direction: column;
  }
  .link-row .el-button {
    width: 100%;
  }
  .detail-head {
    flex-direction: column;
  }
}
</style>
