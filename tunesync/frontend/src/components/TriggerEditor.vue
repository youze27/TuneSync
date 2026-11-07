<template>
  <div class="trigger-editor">
    <el-form label-position="top">
      <el-form-item label="触发方式">
        <el-radio-group :model-value="modelValue.type" @change="(v) => set('type', v)">
          <el-radio-button v-for="t in TYPES" :key="t.key" :value="t.key">{{ t.label }}</el-radio-button>
        </el-radio-group>
        <div class="muted tip">{{ typeTip }}</div>
      </el-form-item>

      <el-form-item v-if="modelValue.type !== 'hourly'" label="执行时间">
        <el-time-picker
          :model-value="modelValue.time"
          format="HH:mm"
          value-format="HH:mm"
          placeholder="选择时间"
          style="width: 160px"
          @change="(v) => set('time', v || '07:00')"
        />
      </el-form-item>

      <el-form-item v-if="modelValue.type === 'weekly'" label="星期（可多选）">
        <div class="row">
          <el-checkbox-group :model-value="modelValue.weekdays" @change="(v) => set('weekdays', v)">
            <el-checkbox-button v-for="d in WEEKDAYS" :key="d.value" :value="d.value">
              {{ d.label }}
            </el-checkbox-button>
          </el-checkbox-group>
          <el-button size="small" text type="primary" @click="set('weekdays', [0, 1, 2, 3, 4])">工作日</el-button>
          <el-button size="small" text @click="set('weekdays', [5, 6])">周末</el-button>
        </div>
      </el-form-item>

      <el-form-item v-if="modelValue.type === 'monthly'" label="每月日期">
        <el-select
          :model-value="String(modelValue.monthday)"
          style="width: 160px"
          @change="(v) => set('monthday', v === 'last' ? 'last' : Number(v))"
        >
          <el-option v-for="d in 31" :key="d" :label="`${d} 日`" :value="String(d)" />
          <el-option label="月末" value="last" />
        </el-select>
      </el-form-item>

      <el-form-item v-if="modelValue.type === 'interval_days'" :label="`间隔天数（从锚点日 ${modelValue.anchor_date} 起算）`">
        <div class="row">
          <el-input-number :model-value="modelValue.interval" :min="1" :max="365" @change="(v) => set('interval', v || 1)" />
          <span class="muted">天</span>
          <el-date-picker
            :model-value="modelValue.anchor_date"
            type="date"
            value-format="YYYY-MM-DD"
            placeholder="锚点日期"
            style="width: 170px"
            @change="(v) => set('anchor_date', v || today)"
          />
        </div>
      </el-form-item>

      <el-form-item v-if="modelValue.type === 'hourly'" label="每 N 小时">
        <el-input-number :model-value="modelValue.interval" :min="1" :max="23" @change="(v) => set('interval', v || 1)" />
      </el-form-item>

      <el-form-item v-if="modelValue.type === 'cron'" label="Cron 表达式（分 时 日 月 周）">
        <el-input
          :model-value="modelValue.cron"
          class="mono"
          placeholder="0 8 * * 1-5"
          @input="(v) => set('cron', v)"
        />
        <div class="muted tip">0=周一 … 6=周日。高级表达式原样交给调度器，不会被可视化控件改写。</div>
      </el-form-item>

      <el-form-item label="错过触发时">
        <el-radio-group :model-value="modelValue.misfire" @change="(v) => set('misfire', v)">
          <el-radio value="skip">直接跳过（推荐）</el-radio>
          <el-radio value="run_once">补跑一次</el-radio>
        </el-radio-group>
        <div v-if="modelValue.misfire === 'run_once'" class="row gap">
          宽限
          <el-input-number
            :model-value="modelValue.grace_minutes"
            :min="1"
            :max="720"
            @change="(v) => set('grace_minutes', v || 60)"
          />
          分钟内补跑
        </div>
      </el-form-item>

      <div class="muted tip">
        时区 {{ timezone }}（由服务端配置决定）。触发时若前面还有别的计划，会进入全局串行队列，实际开始时间可能晚于计划时间。
      </div>
    </el-form>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { state } from '../store.js'

const props = defineProps({
  modelValue: { type: Object, required: true },
})
const emit = defineEmits(['update:modelValue'])

const TYPES = [
  { key: 'daily', label: '每天', tip: '每天在指定时间执行一次。' },
  { key: 'weekly', label: '每周', tip: '可选多天；工作日 / 周末有快捷按钮。' },
  { key: 'monthly', label: '每月', tip: '支持 1–31 日与月末。' },
  { key: 'interval_days', label: '每 N 天', tip: '从锚点日期起算真正的间隔，跨月不会错乱，重启也不会重置。' },
  { key: 'hourly', label: '每 N 小时', tip: '按小时间隔执行，适合高频试跑。' },
  { key: 'cron', label: '自定义', tip: '直接写 cron 表达式，完全交给调度器。' },
]
const WEEKDAYS = [
  { value: 0, label: '一' }, { value: 1, label: '二' }, { value: 2, label: '三' },
  { value: 3, label: '四' }, { value: 4, label: '五' }, { value: 5, label: '六' },
  { value: 6, label: '日' },
]
const today = new Date().toISOString().slice(0, 10)
const timezone = computed(() => state.settings?.server?.timezone || 'Asia/Shanghai')
const typeTip = computed(() => TYPES.find((t) => t.key === props.modelValue.type)?.tip || '')

function set(key, value) {
  emit('update:modelValue', { ...props.modelValue, [key]: value })
}
</script>

<style scoped>
.tip { font-size: 12px; line-height: 1.6; color: var(--el-text-color-secondary); }
.row { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }
.gap { margin-top: 8px; }
</style>
