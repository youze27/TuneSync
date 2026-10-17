<template>
  <div class="page runs-page">
    <el-card shadow="never" class="runs-layout">
      <!-- 左：运行列表 -->
      <div class="runs-list">
        <div class="list-head">
          <span class="list-title">运行历史</span>
          <el-button size="small" text :icon="Refresh" :loading="listLoading" @click="loadList">刷新</el-button>
        </div>
        <div class="list-body scroll-y" v-loading="listLoading">
          <div
            v-for="r in runs"
            :key="r.id"
            class="run-item"
            :class="{ active: r.id === currentId }"
            @click="select(r.id)"
          >
            <div class="run-item-top">
              <span class="run-item-time">{{ fmtTime(r.started_at) }}</span>
              <el-tag size="small" :type="runTag(r.status).type">{{ runTag(r.status).label }}</el-tag>
            </div>
            <div class="run-item-name">{{ r.task_name || r.task_id ? r.task_name || `任务 #${r.task_id}` : '未知任务' }}</div>
            <div class="run-item-sub">
              #{{ r.id }} · {{ fmtDuration(r.started_at, r.finished_at) }}
              <template v-if="summ(r).playlists"> · {{ summ(r).playlists }} 张歌单</template>
              <template v-if="summ(r).total"> · 推送 {{ rateText(summ(r)) }}</template>
            </div>
          </div>
          <div v-if="!runs.length && !listLoading" class="empty-hint">还没有运行记录</div>
        </div>
      </div>

      <!-- 右：运行详情 -->
      <div class="runs-detail" v-loading="detailLoading">
        <template v-if="detail">
          <div class="detail-head">
            <div class="detail-head-main">
              <h3 class="page-title">
                运行 #{{ detail.id }}
                <el-tag size="small" :type="runTag(detail.status).type" style="margin-left: 8px">
                  {{ runTag(detail.status).label }}
                </el-tag>
              </h3>
              <div class="detail-sub">
                任务 {{ detail.task_name || detail.task_id || '—' }} ·
                开始 {{ fmtTime(detail.started_at) }} ·
                结束 {{ fmtTime(detail.finished_at) }} ·
                耗时 {{ fmtDuration(detail.started_at, detail.finished_at) }}
              </div>
            </div>
            <!-- 成功率是这一页最该一眼看到的数，单独放大到右上角 -->
            <div class="head-rate">
              <div class="head-rate-num" :style="{ color: rateColor(s) }">{{ s.total ? ratePct(s) : '—' }}</div>
              <div class="head-rate-cap">推送成功率 · {{ rateText(s) }}</div>
            </div>
          </div>

          <div class="stat-grid detail-stats mb-12">
            <StatCard label="歌单数" :value="s.playlists" icon="Files" color="#409eff" />
            <StatCard label="成功歌曲" :value="s.pushed" icon="CircleCheckFilled" color="#67c23a" />
            <StatCard label="本地复用" :value="s.local" icon="FolderOpened" color="#67c23a" />
            <StatCard label="已下载" :value="s.downloaded" icon="Download" color="#409eff" />
            <StatCard label="已跳过" :value="s.skipped" icon="CircleClose" color="#909399" />
          </div>

          <el-alert
            v-if="s.fatal"
            :title="`运行中断：${s.fatal}`"
            type="error"
            show-icon
            :closable="false"
            class="mb-12"
          />

          <el-alert
            v-if="s.errors.length"
            :title="`本次运行有 ${s.errors.length} 条错误`"
            type="warning"
            show-icon
            class="mb-12"
          >
            <ul class="error-list">
              <li v-for="(e, i) in s.errors" :key="i">{{ e }}</li>
            </ul>
          </el-alert>

          <div class="pl-toolbar">
            <span class="muted">共 {{ playlists.length }} 张歌单</span>
            <el-button size="small" text @click="expandAll">{{ allExpanded ? '全部收起' : '全部展开' }}</el-button>
          </div>

          <el-collapse v-model="expanded">
            <el-collapse-item v-for="p in playlists" :key="p.id" :name="String(p.id)">
              <template #title>
                <div class="pl-title-row">
                  <el-image
                    v-if="p.cover_url"
                    :src="p.cover_url"
                    fit="cover"
                    lazy
                    class="pl-title-cover"
                    :preview-src-list="[p.cover_url]"
                    preview-teleported
                  />
                  <span class="pl-title-name">{{ p.name || '未命名歌单' }}</span>
                  <el-tag size="small" effect="plain">{{ platName(p.platform) }}</el-tag>
                  <el-tag size="small" effect="plain" type="info">{{ catName(p.category) }}</el-tag>
                  <el-tag size="small" :type="plTag(p.status).type">{{ plTag(p.status).label }}</el-tag>
                  <el-tag size="small" type="success" effect="plain">
                    复用 {{ p.local_hit ?? 0 }}
                  </el-tag>
                  <el-tag size="small" type="primary" effect="plain">
                    下载 {{ p.downloaded ?? 0 }}
                  </el-tag>
                  <el-tag size="small" type="info" effect="plain">
                    跳过 {{ p.skipped ?? 0 }}
                  </el-tag>
                  <span class="muted pl-title-jobs">{{ (p.jobs || []).length }} 首</span>
                </div>
              </template>
              <PlaylistCard :playlist="p" :jobs="p.jobs || []" />
            </el-collapse-item>
          </el-collapse>

          <div v-if="!playlists.length" class="empty-hint">这次运行没有产出歌单</div>
        </template>

        <div v-else-if="!detailLoading" class="empty-hint">从左侧选择一个运行记录查看详情</div>
      </div>
    </el-card>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { Refresh } from '@element-plus/icons-vue'
import * as api from '../api.js'
import PlaylistCard from '../components/PlaylistCard.vue'
import StatCard from '../components/StatCard.vue'
import { catName, state } from '../store.js'
import { fmtDuration, fmtTime, playlistStatusTag, runStatusTag, summaryOf } from '../utils.js'

const runs = ref([])
const currentId = ref(null)
const detail = ref(null)
const listLoading = ref(false)
const detailLoading = ref(false)
const expanded = ref([])
const allExpanded = ref(false)

const runTag = (s) => runStatusTag(s)
const plTag = (s) => playlistStatusTag(s)
const summ = (r) => summaryOf(r)
// 平台抓取已删：歌单来源固定是 LLM，其余历史值原样显示
const PLATFORM_NAMES = { llm: 'LLM' }
const platName = (code) => PLATFORM_NAMES[code] || code || '未知平台'

const s = computed(() => summaryOf(detail.value))
const playlists = computed(() => detail.value?.playlists || [])

/** 成功率：「推送成功/歌单总歌曲」 */
const rateText = (x) => `${x.pushed}/${x.total}`
const ratePct = (x) => (x.total ? `${Math.round((x.pushed / x.total) * 100)}%` : '—')
/** 成功率越高越绿，掉到一半以下转警告色，0 目标时保持中性 */
function rateColor(x) {
  if (!x || !x.total) return '#909399'
  const r = x.pushed / x.total
  return r >= 0.9 ? '#67c23a' : r >= 0.5 ? '#e6a23c' : '#f56c6c'
}

async function loadList() {
  listLoading.value = true
  try {
    runs.value = (await api.getRuns(50)) || []
    if (!currentId.value && runs.value.length) await select(runs.value[0].id)
  } catch (e) {
    api.toastError(e, '加载运行历史失败')
  } finally {
    listLoading.value = false
  }
}

async function select(id) {
  currentId.value = id
  detailLoading.value = true
  try {
    detail.value = await api.getRun(id)
    allExpanded.value = false
    expanded.value = []
  } catch (e) {
    api.toastError(e, '加载运行详情失败')
  } finally {
    detailLoading.value = false
  }
}

function expandAll() {
  allExpanded.value = !allExpanded.value
  expanded.value = allExpanded.value ? playlists.value.map((p) => String(p.id)) : []
}

onMounted(loadList)
</script>

<style scoped>
.runs-page {
  height: 100%;
  display: flex;
  flex-direction: column;
}
/* 一张卡片吃满可视高度，左右两栏各自内部滚动 —— 这样两栏底边永远对齐 */
.runs-layout {
  flex: 1;
  min-height: 0;
  display: flex;
  gap: 0;
  overflow: hidden;
}
.runs-layout :deep(.el-card__body) {
  flex: 1;
  min-height: 0;
  display: flex;
  padding: 0;
}
.runs-list {
  width: 300px;
  flex: none;
  min-height: 0;
  border-right: 1px solid #ebeef5;
  display: flex;
  flex-direction: column;
}
.list-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 14px;
  border-bottom: 1px solid #ebeef5;
}
.list-title {
  font-weight: 600;
  font-size: 14px;
}
.list-body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
}
.run-item {
  padding: 10px 14px;
  border-bottom: 1px solid #f2f3f5;
  cursor: pointer;
  transition: background 0.15s;
}
.run-item:hover {
  background: #f5f7fa;
}
.run-item.active {
  background: #ecf5ff;
  box-shadow: inset 3px 0 0 #409eff;
}
.run-item-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
}
.run-item-time {
  font-size: 13px;
  font-weight: 500;
}
.run-item-name {
  font-size: 13px;
  margin-top: 4px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.run-item-sub {
  font-size: 12px;
  color: #909399;
  margin-top: 2px;
}
.runs-detail {
  flex: 1;
  min-width: 0;
  min-height: 0;
  padding: 16px 20px 32px;
  overflow-y: auto;
}
.detail-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 14px;
}
.detail-head-main {
  min-width: 0;
}
.detail-sub {
  color: #909399;
  font-size: 12px;
  margin-top: 4px;
}
.head-rate {
  flex: none;
  text-align: right;
}
.head-rate-num {
  font-size: 30px;
  font-weight: 700;
  line-height: 1.15;
}
.head-rate-cap {
  font-size: 12px;
  color: #909399;
  margin-top: 2px;
}
/* 卡片变多后按整行均分，把右侧留白吃满，不让最后一张缩在中间 */
.detail-stats {
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 12px;
}
.error-list {
  margin: 4px 0 0;
  padding-left: 18px;
  font-size: 12px;
  line-height: 1.8;
  max-height: 140px;
  overflow-y: auto;
}
.pl-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
}
.pl-title-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  min-width: 0;
}
.pl-title-cover {
  width: 32px;
  height: 32px;
  border-radius: 4px;
  flex: none;
  background: #f2f4f7;
}
.pl-title-name {
  font-weight: 600;
  font-size: 13px;
  max-width: 280px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.pl-title-jobs {
  font-size: 12px;
  margin-left: auto;
}
@media (max-width: 900px) {
  /* 窄屏左右分栏会把两侧都挤扁，改成上下堆叠 */
  .runs-layout :deep(.el-card__body) {
    flex-direction: column;
  }
  .runs-list {
    width: 100%;
    max-height: 260px;
    border-right: none;
    border-bottom: 1px solid #ebeef5;
  }
}
</style>