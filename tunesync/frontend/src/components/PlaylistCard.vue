<template>
  <el-card shadow="never" class="pl-card">
    <template #header>
      <div class="pl-head">
        <el-image
          :src="playlist.cover_url"
          fit="cover"
          lazy
          class="pl-cover"
          :preview-src-list="playlist.cover_url ? [playlist.cover_url] : []"
          preview-teleported
        >
          <template #error>
            <div class="pl-cover-fallback">
              <el-icon :size="22"><Picture /></el-icon>
            </div>
          </template>
        </el-image>

        <div class="pl-info">
          <div class="pl-name">{{ playlist.name || '未命名歌单' }}</div>
          <div class="pl-tags">
            <el-tag size="small" effect="plain">{{ platName(playlist.platform) }}</el-tag>
            <el-tag size="small" effect="plain" type="info">{{ catName(playlist.category) }}</el-tag>
            <el-tag size="small" :type="tag.type">{{ tag.label }}</el-tag>
            <el-tag v-if="playlist.source_id" size="small" type="info" effect="plain">
              来源 {{ playlist.source_id }}
            </el-tag>
          </div>
          <div v-if="playlist.error" class="pl-error">
            <el-icon><WarningFilled /></el-icon> {{ playlist.error }}
          </div>
        </div>

        <div class="pl-counts">
          <div class="pl-count">
            <div class="pl-count-num">{{ playlist.track_total ?? 0 }}</div>
            <div class="pl-count-label">曲目</div>
          </div>
          <div class="pl-count">
            <div class="pl-count-num success">{{ playlist.local_hit ?? 0 }}</div>
            <div class="pl-count-label">本地复用</div>
          </div>
          <div class="pl-count">
            <div class="pl-count-num primary">{{ playlist.downloaded ?? 0 }}</div>
            <div class="pl-count-label">已下载</div>
          </div>
          <div class="pl-count">
            <div class="pl-count-num info">{{ playlist.skipped ?? 0 }}</div>
            <div class="pl-count-label">跳过</div>
          </div>
        </div>
      </div>
    </template>

    <el-table v-if="jobs.length" :data="jobs" size="small" border stripe>
      <el-table-column prop="title" label="歌曲" min-width="180" show-overflow-tooltip />
      <el-table-column prop="artist" label="艺术家" min-width="120" show-overflow-tooltip />
      <el-table-column label="状态" width="100">
        <template #default="{ row }">
          <el-tag size="small" :type="st(row.status).type">{{ st(row.status).label }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="source" label="来源" width="120" show-overflow-tooltip />
      <el-table-column label="文件路径" min-width="220">
        <template #default="{ row }">
          <span v-if="row.file_path" class="mono break-all">{{ row.file_path }}</span>
          <span v-else class="muted">—</span>
        </template>
      </el-table-column>
      <el-table-column label="失败原因" min-width="160">
        <template #default="{ row }">
          <span v-if="row.error" class="err-text">{{ row.error }}</span>
          <span v-else class="muted">—</span>
        </template>
      </el-table-column>
    </el-table>
    <div v-else class="empty-hint">暂无歌曲明细</div>
  </el-card>
</template>

<script setup>
import { computed } from 'vue'
import { Picture, WarningFilled } from '@element-plus/icons-vue'
import { catName } from '../store.js'
import { playlistStatusTag, trackStatusTag } from '../utils.js'

const props = defineProps({
  playlist: { type: Object, required: true },
  jobs: { type: Array, default: () => [] },
})

const tag = computed(() => playlistStatusTag(props.playlist.status))
const st = (s) => trackStatusTag(s)
// 平台抓取已删：歌单来源固定是 LLM，其余历史值原样显示
const PLATFORM_NAMES = { llm: 'LLM' }
const platName = (code) => PLATFORM_NAMES[code] || code || '未知平台'
</script>

<style scoped>
.pl-card {
  margin-bottom: 12px;
}
.pl-head {
  display: flex;
  gap: 14px;
  align-items: center;
}
.pl-cover {
  width: 64px;
  height: 64px;
  border-radius: 6px;
  flex: none;
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
.pl-info {
  flex: 1;
  min-width: 0;
}
.pl-name {
  font-size: 15px;
  font-weight: 600;
  margin-bottom: 6px;
  word-break: break-all;
}
.pl-tags {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}
.pl-error {
  margin-top: 6px;
  color: #f56c6c;
  font-size: 12px;
  display: flex;
  align-items: center;
  gap: 4px;
}
.pl-counts {
  display: flex;
  gap: 18px;
  flex: none;
}
.pl-count {
  text-align: center;
}
.pl-count-num {
  font-size: 18px;
  font-weight: 600;
}
.pl-count-num.success {
  color: #67c23a;
}
.pl-count-num.primary {
  color: #409eff;
}
.pl-count-num.info {
  color: #909399;
}
.pl-count-label {
  font-size: 12px;
  color: #909399;
  margin-top: 2px;
}
.err-text {
  color: #f56c6c;
}
</style>