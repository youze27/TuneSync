<template>
  <div>
    <el-form :label-width="labelPosition === 'top' ? '' : '92px'" :label-position="labelPosition">
      <el-form-item label="执行频率">
        <el-radio-group v-model="form.mode" @change="onModeChange">
          <el-radio-button value="daily">每天</el-radio-button>
          <el-radio-button value="interval">每 N 天</el-radio-button>
          <el-radio-button value="weekly">每周</el-radio-button>
          <el-radio-button value="monthly">每月</el-radio-button>
        </el-radio-group>
      </el-form-item>

      <!-- 三个模式各有一个附加字段，容器留出固定高度，切换时下方内容不被顶来顶去 -->
      <div class="cron-dyn" :class="{ tall: labelPosition === 'top' }">
        <el-form-item v-if="form.mode === 'interval'" label="间隔天数">
          <el-input-number v-model="form.interval" :min="1" :max="30" />
          <span class="muted dyn-hint">每 {{ form.interval }} 天执行一次</span>
        </el-form-item>

        <el-form-item v-else-if="form.mode === 'weekly'" label="星期">
          <el-select v-model="form.dow" style="width: 100%">
            <el-option v-for="d in WEEKDAYS" :key="d.value" :label="d.label" :value="d.value" />
          </el-select>
        </el-form-item>

        <el-form-item v-else-if="form.mode === 'monthly'" label="每月几号">
          <el-input-number v-model="form.dom" :min="1" :max="28" />
          <span class="muted dyn-hint">取 1–28，避免月末短月跳过</span>
        </el-form-item>
      </div>

      <el-form-item label="执行时间">
        <el-time-picker
          v-model="form.time"
          format="HH:mm"
          value-format="HH:mm"
          placeholder="选择时间"
          style="width: 160px"
        />
      </el-form-item>

      <el-form-item v-if="showExpr" label="Cron 表达式">
        <el-tag size="small" type="info" class="mono">{{ modelValue || '—' }}</el-tag>
        <!-- 编号约定由后端 GET /api/settings 的 cron 字段下发，这里不再硬编码第二份 -->
        <div v-if="cron" class="muted" style="font-size: 12px; margin-top: 4px">
          <div>字段顺序：<span class="mono">{{ cron.fields }}</span></div>
          <div>星期编号：{{ cron.day_of_week }}</div>
          <div>示例：<span class="mono">{{ cron.example }}</span></div>
        </div>
      </el-form-item>
    </el-form>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, watch } from 'vue'
import { cronHelp, ensureSettings, weekdayLabels } from '../store.js'

/** 后端公布的 cron 说明文案；未加载到时为 null（模板里对应区域不渲染）。 */
const cron = computed(() => cronHelp.value)

/**
 * 星期下拉：编号 0–6，名称取自后端 cron.day_of_week 公布的约定。
 * 后端把星期编号换成另一套时，这里跟着变，不用改前端。
 */
const WEEKDAYS = computed(() => weekdayLabels.value.map((label, value) => ({ label, value })))

const props = defineProps({
  modelValue: { type: String, default: '' },
  // 推送配置页只想要「每天 / 每周几点」这套人话，不展示底层 cron 字符串
  showExpr: { type: Boolean, default: true },
  // 嵌在 label-position="top" 的表单里时传 top，内部表单跟着对齐
  labelPosition: { type: String, default: 'right' },
})
const emit = defineEmits(['update:modelValue'])

const form = reactive({
  mode: 'daily',
  time: '03:00',
  interval: 2,
  dow: 0,
  dom: 1,
})

const pad = (n) => String(n).padStart(2, '0')

/** 由当前表单拼出 5 段 cron。 */
function buildCron() {
  const [hh, mm] = (form.time || '03:00').split(':')
  const min = mm ?? '00'
  const hour = hh ?? '03'
  switch (form.mode) {
    case 'interval':
      return `${min} ${hour} */${form.interval} * *`
    case 'weekly':
      return `${min} ${hour} * * ${form.dow}`
    case 'monthly':
      return `${min} ${hour} ${form.dom} * *`
    case 'daily':
    default:
      return `${min} ${hour} * * *`
  }
}

function emitCron() {
  emit('update:modelValue', buildCron())
}

/** 解析已有 cron 表达式回填表单；识别不出时退回「每天」并保留原表达式。 */
function parseCron(expr) {
  const parts = String(expr || '').trim().split(/\s+/)
  if (parts.length !== 5) {
    form.mode = 'daily'
    form.time = '03:00'
    return
  }
  const [min, hour, dom, mon, dow] = parts
  form.time = `${pad(Number(hour) || 0)}:${pad(Number(min) || 0)}`

  if (mon !== '*') return // 月字段非 * 的表达式不在 UI 覆盖范围内

  const domStep = /^\*\/(\d+)$/.exec(dom)
  if (domStep) {
    form.mode = 'interval'
    form.interval = Number(domStep[1]) || 2
  } else if (dom !== '*') {
    form.mode = 'monthly'
    form.dom = Number(dom) || 1
  } else if (dow !== '*' && /^\d$/.test(dow)) {
    form.mode = 'weekly'
    form.dow = Number(dow)
  } else {
    form.mode = 'daily'
  }
}

function onModeChange() {
  emitCron()
}

watch(
  () => props.modelValue,
  (v) => parseCron(v),
  { immediate: true },
)

watch(
  () => [form.mode, form.time, form.interval, form.dow, form.dom],
  () => emitCron(),
)

onMounted(() => {
  // 父页面通常已在 boot() 里预取过；这里只在没有缓存时补一次，不阻塞渲染
  if (!cron.value) ensureSettings().catch(() => {})
})

defineExpose({ WEEKDAYS })
</script>

<style scoped>
/* 三个模式共用的插槽容器：固定高度，切换模式时下面的「执行时间」不会被顶来顶去 */
.cron-dyn {
  min-height: 32px;
}
/* 标签在上方时，附加字段要连标签一起换行，留两行高度 */
.cron-dyn.tall {
  min-height: 72px;
}
.dyn-hint {
  margin-left: 8px;
}

/* 直接子选择器压过父页面 .cfg-form :deep(.el-form-item) 的默认外边距，避免叠加出多余空隙 */
.cron-dyn > .el-form-item {
  margin-bottom: 0;
}

.cron-dyn > .el-form-item > .el-form-item__label {
  padding-bottom: 4px;
  font-size: 13px;
}

.cron-dyn :deep(.el-input-number) {
  width: 130px;
}
</style>