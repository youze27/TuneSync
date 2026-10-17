<template>
  <div class="stat-card">
    <div class="stat-head">
      <el-icon :size="18" :color="color"><component :is="icon" /></el-icon>
      <span class="stat-label">{{ label }}</span>
    </div>
    <div class="stat-value" :style="{ color }">
      <template v-if="loading">
        <el-skeleton :rows="1" animated />
      </template>
      <template v-else>{{ value }}</template>
    </div>
    <div class="stat-extra">
      <slot name="extra">
        <span class="muted">{{ hint }}</span>
      </slot>
    </div>
  </div>
</template>

<script setup>
defineProps({
  label: { type: String, required: true },
  value: { type: [String, Number], default: '—' },
  hint: { type: String, default: '' },
  icon: { type: [String, Object], default: 'DataLine' },
  color: { type: String, default: '#303133' },
  loading: { type: Boolean, default: false },
})
</script>

<style scoped>
.stat-card {
  background: #fff;
  border: 1px solid #ebeef5;
  border-radius: 8px;
  padding: 14px 16px;
  min-height: 104px;
}
.stat-head {
  display: flex;
  align-items: center;
  gap: 6px;
  color: #909399;
  font-size: 13px;
}
.stat-value {
  font-size: 24px;
  font-weight: 600;
  margin-top: 8px;
  line-height: 1.2;
  word-break: break-all;
}
.stat-extra {
  margin-top: 6px;
  font-size: 12px;
  color: #909399;
  line-height: 1.6;
}
</style>