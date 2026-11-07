<template>
  <el-card shadow="never" class="preview-card" v-loading="loading">
    <template #header>
      <div class="head">
        <span class="title">执行预览</span>
        <el-tag v-if="dirty" size="small" type="warning" effect="plain">有未保存改动</el-tag>
        <el-tag v-else size="small" type="success" effect="plain">已保存</el-tag>
      </div>
    </template>

    <div v-if="error" class="err">{{ error }}</div>
    <template v-else-if="preview">
      <div class="line"><span class="k">触发</span><span class="v">{{ preview.trigger_text }}</span></div>
      <div class="line"><span class="k">接下来</span>
        <span class="v mono">
          <div v-for="(t, i) in preview.next_runs" :key="i">{{ t }}</div>
          <span v-if="!preview.next_runs?.length" class="muted">无法计算</span>
        </span>
      </div>
      <el-divider />
      <div class="line"><span class="k">来源</span><span class="v">{{ preview.source_text }}</span></div>
      <div v-if="preview.search_text" class="line">
        <span class="k">搜索词</span><span class="v">{{ preview.search_text }}<span class="muted">（由场景自动决定）</span></span>
      </div>
      <div class="line"><span class="k">每次产出</span><span class="v"><b>{{ preview.playlists_per_run }}</b> 张歌单</span></div>
      <div class="line"><span class="k">每歌单</span><span class="v">约 {{ preview.tracks_per_playlist }} 首</span></div>
      <div class="line"><span class="k">下载上限</span><span class="v">约 {{ preview.max_downloads }} 首</span></div>
      <div class="line"><span class="k">命名示例</span><span class="v break-all">{{ preview.name_example }}</span></div>

      <el-alert
        v-for="(w, i) in preview.warnings || []"
        :key="i"
        type="warning"
        :closable="false"
        show-icon
        :title="w"
        class="mt-8"
      />
      <el-alert type="info" :closable="false" show-icon class="mt-8" title="歌单名保持源歌单原名；已推过的歌单会被排除，不会重样。" />
    </template>
    <div v-else class="muted">正在计算预览…</div>
  </el-card>
</template>

<script setup>
defineProps({
  preview: { type: Object, default: null },
  loading: { type: Boolean, default: false },
  dirty: { type: Boolean, default: false },
  error: { type: String, default: '' },
})
</script>

<style scoped>
.preview-card { position: sticky; top: 12px; }
.head { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.title { font-weight: 600; font-size: 14px; }
.line { display: flex; gap: 10px; font-size: 13px; line-height: 1.8; }
.k { flex: 0 0 68px; color: var(--el-text-color-secondary); }
.v { flex: 1; min-width: 0; }
.mono { font-variant-numeric: tabular-nums; }
.break-all { word-break: break-all; }
.muted { color: var(--el-text-color-secondary); }
.err { color: var(--el-color-danger); font-size: 13px; line-height: 1.6; }
.mt-8 { margin-top: 8px; }
</style>
