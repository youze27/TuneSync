<template>
  <!-- 独立页面：把整块可用高度交给聊天窗口，页头只占一行 -->
  <div class="ai-page">
    <AIAgent />
  </div>
</template>

<script setup>
import AIAgent from '../components/AIAgent.vue'
</script>

<style scoped>
.ai-page {
  height: 100%;
  min-height: calc(100vh - 52px);
  min-height: calc(100dvh - 52px);
  box-sizing: border-box;
  display: flex;
  padding: 10px 12px 12px;
}
.ai-page :deep(.ai-agent) {
  flex: 1;
  min-width: 0;
  min-height: 0;
  display: flex;
  flex-direction: column;
  margin: 0;
}
.ai-page :deep(.chat-scroll) {
  flex: 1;
  height: auto;
  min-height: 0;
}

@media (max-width: 768px) {
  .ai-page { padding: 0; }
  .ai-page :deep(.ai-agent) { border: none; border-radius: 0; box-shadow: none; }
}
</style>
