<template>
  <el-tag :type="tag.type" size="small" effect="light">{{ tag.label }}</el-tag>
</template>

<script setup>
import { computed } from 'vue'
import { healthTag } from '../utils.js'

const props = defineProps({
  health: { type: String, default: '' },
})
const tag = computed(() => healthTag(props.health))
</script>