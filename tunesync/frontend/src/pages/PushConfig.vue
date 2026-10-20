<template>
  <div class="page pc-page" v-loading="loading">
    <div class="page-header">
      <div>
        <h2 class="page-title">{{ editing ? `编辑推送计划 #${editing.id}` : '新建推送计划' }}</h2>
        <div class="page-subtitle">
          一条计划 = 场景 + 歌单来源 + 执行频率；歌单命名与下载参数在全局设置里统一维护
        </div>
      </div>
      <div class="toolbar">
        <el-button :icon="ArrowLeft" @click="goBack">返回</el-button>
        <el-button :icon="RefreshLeft" :loading="loading" @click="load">重载</el-button>
        <el-button :icon="Check" :loading="saving && !thenRun" @click="submit(false)">保存</el-button>
        <el-button type="primary" :icon="VideoPlay" :loading="saving && thenRun" @click="submit(true)">
          保存并立即执行
        </el-button>
      </div>
    </div>

    <el-alert v-if="saveError" type="error" show-icon class="mb-12" @close="saveError = ''">
      <template #title>{{ saveError }}</template>
    </el-alert>

    <el-form ref="formRef" :model="draft" :rules="rules" label-position="top" class="cfg-form">
      <!-- 基本信息：挤成一条横栏，计划名 / 启用 / 频率各占一列 -->
      <el-card shadow="never" class="mb-12 basic-card">
        <template #header>
          <div class="card-head">
            <span class="card-title"><el-icon><EditPen /></el-icon> 基本信息</span>
            <span class="muted basic-cron" :title="cronDesc">{{ cronDesc }}</span>
          </div>
        </template>

        <div class="basic-bar">
          <el-form-item label="计划名称" prop="name">
            <el-input v-model="draft.name" maxlength="64" placeholder="例如：每日热歌推送" />
          </el-form-item>

          <el-form-item label="启用状态">
            <div class="row-inline">
              <el-switch v-model="draft.enabled" />
              <span class="muted tip-one" :title="enabledTip">{{ enabledTip }}</span>
            </div>
          </el-form-item>

          <!-- 频率选择器改用右对齐标签，内部三行压成紧凑的三行小控件 -->
          <div class="b-cron">
            <CronPicker v-model="draft.cron_expr" :show-expr="false" label-position="right" />
          </div>
        </div>
      </el-card>

      <el-row :gutter="12">
        <!-- 推送场景 -->
        <el-col :xs="24" :lg="12">
          <el-card shadow="never" class="mb-12">
            <template #header>
              <div class="card-head">
                <span class="card-title"><el-icon><Collection /></el-icon> 推送场景</span>
                <div class="toolbar">
                  <el-button size="small" text @click="selectAll(true)">全选</el-button>
                  <el-button size="small" text @click="selectAll(false)">全不选</el-button>
                  <el-button size="small" text type="warning" @click="invert">反选</el-button>
                </div>
              </div>
            </template>

            <div class="cat-scroll">
              <el-alert v-if="staleCodes.length" type="warning" show-icon class="mb-12">
                <template #title>服务端偏好里有已下线的项，保存时会被丢弃</template>
                <template #default>{{ staleCodes.join('、') }} 已不在当前场景字典中</template>
              </el-alert>

              <el-alert
                v-if="!groupRows.length && !loading"
                type="error"
                show-icon
                :closable="false"
                title="场景字典是空的，没法勾选"
              >
                <template #default>服务端 GET /api/categories 没有返回场景，检查 config/categories.yaml 后点右上角「重载」</template>
              </el-alert>

              <div v-for="g in groupRows" :key="g.key" class="group-block" :class="{ special: g.key === 'special' }">
                <div class="group-title">
                  <el-tag v-if="g.key === 'special'" size="small" type="primary" effect="dark">置顶</el-tag>
                  <span>{{ g.label }}</span>
                  <span class="muted group-count">已选 {{ pickedIn(g.key) }} / {{ g.items.length }}</span>
                  <el-button size="small" text @click="toggleGroup(g.key)">全选/反选</el-button>
                </div>
                <div class="cat-group">
                  <el-checkbox
                    v-for="c in g.items"
                    :key="c.code"
                    :model-value="draft.categories.includes(c.code)"
                    border
                    @change="(on) => toggleOne(c.code, on)"
                  >
                    {{ c.name }}
                  </el-checkbox>
                </div>
              </div>
            </div>

            <template #footer>
              <span class="muted">已选 {{ validCategories.length }} / {{ state.categories.length }} 个场景</span>
            </template>
          </el-card>
        </el-col>

        <!-- 推送偏好：左侧竖排选项卡选来源，右侧只显示该来源自己的配置 -->
        <el-col :xs="24" :lg="12">
          <el-card shadow="never" class="mb-12 pref-card">
            <template #header>
              <span class="card-title"><el-icon><Operation /></el-icon> 推送偏好</span>
            </template>

            <div class="pref-split">
              <el-radio-group v-model="draft.source" class="pref-nav">
                <el-radio-button v-for="m in SOURCE_MODES" :key="m.key" :value="m.key">
                  <span class="pref-nav-label">{{ m.label }}</span>
                  <span class="pref-nav-desc">{{ shortDesc(m.key) }}</span>
                </el-radio-button>
              </el-radio-group>

              <div class="pref-pane">
                <div class="muted pane-desc" :title="sourceDesc">{{ sourceDesc }}</div>

                <!-- AI 生成：歌单和曲目都由模型产出 -->
                <template v-if="isAi">
                  <div class="grid-2">
                    <el-form-item label="场景推送">
                      <el-radio-group v-model="draft.scene_mode" class="mode-group">
                        <el-radio-button v-for="m in SCENE_MODES" :key="m.key" :value="m.key">
                          {{ m.label }}
                        </el-radio-button>
                      </el-radio-group>
                      <div class="muted tip-one" :title="sceneModeDesc">{{ sceneModeDesc }}</div>
                    </el-form-item>

                    <el-form-item label="每歌单歌曲数">
                      <el-select v-model="draft.tracks_per" style="width: 100%">
                        <el-option
                          v-for="o in tracksPerOptions"
                          :key="o.value"
                          :label="o.label"
                          :value="o.value"
                        />
                      </el-select>
                      <div class="muted tip-one">每张歌单的曲目数</div>
                    </el-form-item>

                    <el-form-item label="每场景歌单数" prop="count_per" class="span-2">
                      <el-input-number
                        v-model="draft.count_per"
                        :min="1"
                        :max="50"
                        controls-position="right"
                        :disabled="isMerge"
                      />
                      <span class="muted tip-one inline" :title="countPerTip">{{ countPerTip }}</span>
                    </el-form-item>
                  </div>

                  <el-form-item label="自定义提示词">
                    <el-input
                      v-model="draft.custom_prompt"
                      type="textarea"
                      :rows="3"
                      maxlength="2000"
                      show-word-limit
                      placeholder="例如：适合深夜开车听的国风电子，节奏要稳，带一点武侠感"
                    />
                    <div class="muted tip">填写后按提示词生成歌单（场景仅作兜底主题）；留空则完全按勾选的场景推送</div>
                  </el-form-item>
                </template>

                <!-- 乐源拉取：先从平台取真实歌单，AI 筛选可关掉直接推送 -->
                <template v-else>
                  <el-form-item label="来源平台">
                    <el-checkbox-group v-model="draft.source_platforms" class="src-plats">
                      <el-checkbox v-for="s in SOURCE_PLATFORMS" :key="s.key" :value="s.key" border>
                        {{ s.name }}
                      </el-checkbox>
                    </el-checkbox-group>
                    <div class="muted tip-one">至少留一个；勾「全部」等价于不限平台</div>
                  </el-form-item>

                  <el-form-item label="AI 筛选">
                    <div class="row-inline">
                      <el-switch v-model="draft.aiFilter" />
                      <span class="muted tip-one" :title="aiFilterTip">{{ aiFilterTip }}</span>
                    </div>
                  </el-form-item>

                  <!-- 开了 AI 筛选才有「场景怎么推」的选择：单独=每场景各筛各的，合并=一次送审 -->
                  <el-form-item v-if="draft.aiFilter" label="场景推送">
                    <el-radio-group v-model="draft.scene_mode" class="mode-group">
                      <el-radio-button v-for="m in SCENE_MODES" :key="m.key" :value="m.key">
                        {{ m.label }}
                      </el-radio-button>
                    </el-radio-group>
                    <div class="muted tip-one" :title="sceneModeDesc">{{ sceneModeDesc }}</div>
                  </el-form-item>

                  <el-form-item label="每场景歌单数" prop="count_per">
                    <el-input-number
                      v-model="draft.count_per"
                      :min="1"
                      :max="50"
                      controls-position="right"
                      :disabled="draft.aiFilter && isMerge"
                    />
                    <div class="muted tip-one">{{ countPerSourceTip }}</div>
                  </el-form-item>

                  <el-form-item label="搜索关键词">
                    <el-input
                      v-model="draft.custom_prompt"
                      type="textarea"
                      :rows="3"
                      maxlength="2000"
                      show-word-limit
                      placeholder="例如：深夜 国风电子；留空则直接用场景名当关键词去搜"
                    />
                    <div class="muted tip">留空就用勾选的场景名当搜索词；关掉 AI 筛选时，关键词只影响去哪搜，不参与挑选</div>
                  </el-form-item>
                </template>
              </div>
            </div>
          </el-card>
        </el-col>
      </el-row>
    </el-form>

    <div class="foot-bar">
      <span class="muted">
        <router-link to="/settings">全局设置</router-link>里可改歌单命名与下载参数，对所有计划生效
      </span>
      <div class="toolbar">
        <el-button @click="goBack">返回</el-button>
        <el-button :loading="saving && !thenRun" @click="submit(false)">保存</el-button>
        <el-button type="primary" :loading="saving && thenRun" @click="submit(true)">
          保存并立即执行
        </el-button>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import {
  ArrowLeft, Check, Collection, EditPen, Operation, RefreshLeft, VideoPlay,
} from '@element-plus/icons-vue'
import * as api from '../api.js'
import CronPicker from '../components/CronPicker.vue'
import {
  GROUP_LABELS,
  GROUP_ORDER,
  SCENE_MODES,
  SOURCE_MODES,
  SOURCE_PLATFORMS,
  TRACKS_PER_OPTIONS,
  boot,
  ensureSettings,
  groupedCategories,
  loadDictionary,
  loadTasks,
  invalidateSettings,
  normaliseSourcePlatforms,
  preferences,
  pushModeOf,
  splitPushMode,
  state,
  weekdayLabels,
} from '../store.js'

const route = useRoute()
const router = useRouter()

const loading = ref(false)
const saving = ref(false)
const thenRun = ref(false)
const saveError = ref('')
const formRef = ref(null)

const editing = ref(null)

const tasks = computed(() => state.tasks)
const grouped = computed(() => groupedCategories.value)

/** 场景按分组平铺，全部常驻可见；以前藏在折叠面板里，默认只展开一组，看着像没加载出来。 */
const groupRows = computed(() =>
  GROUP_ORDER.filter((g) => grouped.value[g]?.length).map((g) => ({
    key: g,
    label: GROUP_LABELS[g] || g,
    items: grouped.value[g],
  })),
)

const blank = () => ({
  name: '',
  categories: [],
  custom_prompt: '',
  count_per: 3,
  cron_expr: '0 3 * * *',
  enabled: true,
  // 界面上拆成「歌单来源 + AI 筛选」，提交时合成后端的 push_mode
  source: 'ai',
  aiFilter: true,
  source_platforms: ['all'],
  scene_mode: 'separate',
  tracks_per: 0,
})
const draft = reactive(blank())

const rules = {
  name: [{ required: true, message: '请填写计划名称', trigger: 'blur' }],
  categories: [{ type: 'array', required: true, min: 1, message: '至少选择一个场景', trigger: 'change' }],
  count_per: [{ type: 'number', required: true, message: '请填写每场景歌单数', trigger: 'change' }],
}

const isAi = computed(() => draft.source === 'ai')
/** 合并推送把所有场景合成一个歌单，「每场景歌单数」对它没有意义。 */
const isMerge = computed(() => draft.scene_mode === 'merge')

const enabledTip = computed(() =>
  draft.enabled ? '按下面的频率自动触发' : '保留计划，但不再按频率触发',
)

const sourceDesc = computed(() => SOURCE_MODES.find((m) => m.key === draft.source)?.desc || '')

/** 左侧选项卡上那行小字，一眼区分两条取歌单的路径。 */
function shortDesc(key) {
  return key === 'ai' ? '模型直接创作' : '平台歌单拉取'
}

const aiFilterTip = computed(() =>
  draft.aiFilter ? '在拉来的歌单里再让模型筛一轮' : '不经过 AI，按平台热度直接推送',
)

const sceneModeDesc = computed(
  () => SCENE_MODES.find((m) => m.key === draft.scene_mode)?.desc || '',
)

const countPerTip = computed(() =>
  isMerge.value ? '合并推送只产出一张综合歌单，这里不生效' : '每个场景生成多少张歌单（1–50）',
)

/** 乐源拉取分支里「每场景歌单数」的提示：随 AI 筛选/场景模式变化。 */
const countPerSourceTip = computed(() => {
  if (!draft.aiFilter) return '关掉 AI 筛选时按平台热度取前 N 张（每个场景）'
  if (isMerge.value) return '合并筛选时由模型统一按场景挑选，这里不生效'
  return '每个场景要几张歌单（1–50），由 AI 按场景挑选'
})

const cronDesc = computed(() => describeCron(draft.cron_expr))

/** 0 项显示为「跟随全局」，让人一眼知道不选就是用全局设置里那张歌单的曲目数。 */
const tracksPerOptions = computed(() => {
  const globalPer = Number(state.settings?.llm?.tracks_per_playlist) || 30
  return TRACKS_PER_OPTIONS.map((n) => ({
    value: n,
    label: n === 0 ? `跟随全局（${globalPer} 首）` : `${n} 首`,
  }))
})

/** 把 cron 翻成人话；识别不出就原样返回。星期名沿用后端公布的编号约定。 */
function describeCron(expr) {
  const p = String(expr || '').trim().split(/\s+/)
  if (p.length !== 5) return '自定义'
  const [min, hour, dom, mon, dow] = p
  const WD = weekdayLabels.value
  const hm = `${hour.padStart(2, '0')}:${String(min).padStart(2, '0')}`
  if (mon === '*' && dom === '*' && dow === '*') return `每天 ${hm}`
  const step = /^\*\/(\d+)$/.exec(dom)
  if (mon === '*' && step && dow === '*') return `每 ${step[1]} 天 ${hm}`
  if (mon === '*' && /^\d+$/.test(dom) && dow === '*') return `每月 ${dom} 日 ${hm}`
  if (mon === '*' && dom === '*' && /^\d$/.test(dow)) return `每${WD[Number(dow)] ?? dow} ${hm}`
  return '自定义'
}

/* ──────────────────────────── 场景勾选 ──────────────────────────── */

function selectAll(fill) {
  draft.categories = fill ? state.categories.map((c) => c.code) : []
}

function toggleGroup(g) {
  const codes = (grouped.value[g] || []).map((c) => c.code)
  const allIn = codes.every((c) => draft.categories.includes(c))
  const next = new Set(draft.categories)
  codes.forEach((c) => (allIn ? next.delete(c) : next.add(c)))
  draft.categories = [...next]
}

/** 单个场景勾选。各组共用一个 draft.categories，逐个受控写入互不干扰。 */
function toggleOne(code, on) {
  const next = new Set(draft.categories)
  if (on) next.add(code)
  else next.delete(code)
  draft.categories = [...next]
}

/** 某组已勾了几个，用于分组标题上的计数。 */
function pickedIn(g) {
  const codes = grouped.value[g] || []
  return codes.filter((c) => draft.categories.includes(c.code)).length
}

function invert() {
  const cur = new Set(draft.categories)
  draft.categories = state.categories.filter((c) => !cur.has(c.code)).map((c) => c.code)
}

/** 真正存在于当前场景字典里的勾选项，提交时按它过滤。 */
const validCategories = computed(() =>
  draft.categories.filter((c) => state.categories.some((x) => x.code === c)),
)

/**
 * 字典里已经下线的场景（后端会直接 400 拒绝），提交前过滤掉，
 * 并把被丢掉的项提示出来，避免用户以为「保存没生效」。
 */
const staleCodes = computed(() =>
  draft.categories.filter((c) => !state.categories.some((x) => x.code === c)),
)

/* ──────────────────────────── 载入 / 提交 ──────────────────────────── */

function fillFromTask(row) {
  const { source, aiFilter } = splitPushMode(row.push_mode)
  Object.assign(draft, {
    name: row.name || '',
    categories: Array.isArray(row.categories) ? [...row.categories] : [],
    custom_prompt: row.custom_prompt || '',
    count_per: row.count_per ?? 3,
    cron_expr: row.cron_expr || '0 3 * * *',
    enabled: !!row.enabled,
    source,
    aiFilter,
    source_platforms: normaliseSourcePlatforms(row.source_platforms),
    scene_mode: row.scene_mode === 'merge' ? 'merge' : 'separate',
    tracks_per: Number.isInteger(row.tracks_per) ? row.tracks_per : 0,
  })
}

/** 新建时用服务端 preferences 当草稿起点；用户改了只影响这条计划。 */
function fillBlankFromPrefs() {
  Object.assign(draft, blank())
  const pref = preferences.value
  if (pref.categories?.length) draft.categories = [...pref.categories]
  if (pref.count_per) draft.count_per = pref.count_per
  if (pref.custom_prompt) draft.custom_prompt = pref.custom_prompt
  if (pref.push_mode) {
    const { source, aiFilter } = splitPushMode(pref.push_mode)
    draft.source = source
    draft.aiFilter = aiFilter
  }
  if (pref.source_platforms) draft.source_platforms = normaliseSourcePlatforms(pref.source_platforms)
}

async function load() {
  loading.value = true
  try {
    if (!state.booted) await boot()
    // boot() 已经跑过但字典那一把失败过时 state.categories 会是空的，
    // 这里再补一次，否则整个场景区渲染成空白，看起来像「选项没加载出来」。
    if (!state.categories.length) await loadDictionary().catch(() => {})
    await Promise.all([ensureSettings(), loadTasks().catch(() => {})])

    const id = Number(route.query.id)
    const row = id ? tasks.value.find((t) => t.id === id) : null
    if (id && !row) {
      ElMessage.warning(`计划 #${id} 不存在，已按新建处理`)
    }
    editing.value = row || null
    if (row) fillFromTask(row)
    else fillBlankFromPrefs()
    state.error = ''
  } catch (e) {
    api.toastError(e, '加载推送配置失败')
  } finally {
    loading.value = false
  }
}

function goBack() {
  router.push('/tasks')
}

async function submit(runAfter) {
  const ok = await formRef.value?.validate().catch(() => false)
  if (!ok) return
  saving.value = true
  thenRun.value = runAfter
  saveError.value = ''
  try {
    const body = {
      name: draft.name,
      platforms: [],
      categories: validCategories.value,
      custom_prompt: (draft.custom_prompt || '').trim(),
      count_per: draft.count_per,
      cron_expr: draft.cron_expr,
      enabled: draft.enabled,
      push_mode: pushModeOf(draft.source, draft.aiFilter),
      source_platforms: normaliseSourcePlatforms(draft.source_platforms),
      scene_mode: draft.scene_mode,
      tracks_per: draft.tracks_per || 0,
    }

    // preferences 是整节覆盖，必须把场景/提示词/歌单数一起带上，
    // 否则只写 push_mode 会把服务端已有的其它默认值抹掉
    await api.putSettings({
      preferences: {
        categories: body.categories,
        count_per: body.count_per,
        custom_prompt: body.custom_prompt,
        push_mode: body.push_mode,
        source_platforms: body.source_platforms,
      },
    })
    invalidateSettings()

    const saved = editing.value
      ? await api.updateTask(editing.value.id, body)
      : await api.createTask(body)
    const taskId = editing.value?.id ?? saved?.id

    await loadTasks()
    if (runAfter && taskId) {
      await api.runTask(taskId)
      ElMessage.success('计划已保存并开始执行')
      router.push('/')
      return
    }
    ElMessage.success(editing.value ? '计划已更新' : '计划已创建')
    if (editing.value) router.push('/tasks')
    else router.replace({ path: '/push-config', query: { id: String(taskId) } })
  } catch (e) {
    saveError.value = api.errText(e, '保存失败')
  } finally {
    saving.value = false
    thenRun.value = false
  }
}

onMounted(load)
</script>

<style scoped>
.pc-page {
  padding-bottom: 20px;
}
.card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  flex-wrap: wrap;
}
.card-title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-weight: 600;
  font-size: 14px;
}
.cfg-form :deep(.el-form-item) {
  margin-bottom: 12px;
}
.cfg-form :deep(.el-form-item__label) {
  padding-bottom: 2px;
  font-size: 13px;
  color: #606266;
}
/* 卡片内边距收一收，基本信息一屏放得下 */
.cfg-form :deep(.el-card__body) {
  padding: 12px 16px 0;
}
.cfg-form :deep(.el-card__body > .el-form-item:last-child) {
  margin-bottom: 12px;
}
/* 基本信息压成一条横栏：计划名 / 启用 / 频率 各占一列，整张卡片只占一行多 */
.basic-bar {
  display: grid;
  grid-template-columns: minmax(200px, 1.1fr) minmax(220px, 1fr) minmax(0, 1.5fr);
  gap: 0 20px;
  align-items: start;
}
.basic-cron {
  font-size: 12px;
  line-height: 18px;
  font-variant-numeric: tabular-nums;
}
.b-cron :deep(.el-form-item) {
  margin-bottom: 6px;
}
.b-cron :deep(.el-form-item__label) {
  width: 68px !important;
  padding-bottom: 6px;
  font-size: 13px;
}
.grid-2 {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 0 16px;
}
.span-2 {
  grid-column: 1 / -1;
}
.row-inline {
  display: flex;
  align-items: center;
  gap: 10px;
  min-height: 32px;
}
.tip {
  font-size: 12px;
  line-height: 1.6;
}
/*
 * 单行固定高度的说明文案。开关/单选切状态时文案会换，
 * 高度不变才不会把下面的表单项顶来顶去（长文案省略号，完整内容看 title）。
 */
.tip-one {
  font-size: 12px;
  line-height: 18px;
  height: 18px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.tip-one.inline {
  margin-left: 10px;
}
.mode-group {
  display: flex;
  flex-wrap: wrap;
}
/* 推送偏好：左窄右宽的两栏，左边竖排选项卡选歌单来源，右边只放该来源的配置 */
.pref-split {
  display: grid;
  grid-template-columns: 132px minmax(0, 1fr);
  gap: 0 14px;
  align-items: start;
}
.pref-nav {
  display: flex;
  flex-direction: column;
  gap: 8px;
  width: 100%;
}
.pref-nav :deep(.el-radio-button) {
  width: 100%;
}
.pref-nav :deep(.el-radio-button__inner) {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 2px;
  width: 100%;
  padding: 9px 11px;
  border: 1px solid var(--el-border-color);
  border-radius: 6px !important;
  box-shadow: none !important;
  white-space: normal;
}
.pref-nav :deep(.el-radio-button:first-child .el-radio-button__inner) {
  border-radius: 6px !important;
}
.pref-nav :deep(.el-radio-button__original-radio:checked + .el-radio-button__inner) {
  border-color: var(--el-color-primary);
  background: var(--el-color-primary-light-9);
  box-shadow: none !important;
}
.pref-nav-label {
  font-size: 13px;
  font-weight: 600;
  line-height: 18px;
  color: var(--el-text-color-primary);
}
.pref-nav :deep(.el-radio-button__original-radio:checked + .el-radio-button__inner) .pref-nav-label {
  color: var(--el-color-primary);
}
.pref-nav-desc {
  font-size: 11px;
  line-height: 15px;
  color: var(--el-text-color-placeholder);
}
.pref-pane {
  min-width: 0;
  border-left: 1px solid var(--el-border-color-lighter);
  padding-left: 14px;
}
.pane-desc {
  font-size: 12px;
  line-height: 18px;
  margin-bottom: 10px;
}
.cat-scroll {
  max-height: 52vh;
  overflow-y: auto;
  padding-right: 4px;
}
.src-plats {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 10px;
}
.src-plats :deep(.el-checkbox) {
  margin-right: 0;
}
.group-block {
  padding-bottom: 10px;
}
.group-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  font-weight: 600;
  margin-bottom: 8px;
}
.group-count {
  font-size: 12px;
  font-weight: 400;
}
.special {
  background: #f7fafd;
  border: 1px solid #e4ecf7;
  border-radius: 6px;
  padding: 10px 12px;
}
.cat-group {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 10px;
}
.cat-group :deep(.el-checkbox) {
  margin-right: 0;
}
/* 底栏跟着内容滚，不遮侧边栏；sticky 才认得 el-main 这个滚动容器 */
.foot-bar {
  position: sticky;
  bottom: 0;
  z-index: 10;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  margin-top: 12px;
  padding: 10px 16px;
  background: rgba(255, 255, 255, 0.94);
  backdrop-filter: blur(6px);
  border: 1px solid #ebeef5;
  border-radius: 8px;
  box-shadow: 0 -2px 10px rgba(0, 0, 0, 0.04);
  font-size: 12px;
}

@media (max-width: 1180px) {
  /* 屏幕不够宽时左右两栏先叠起来，否则右边的配置被挤得换行 */
  .basic-bar {
    grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  }
  .b-cron {
    grid-column: 1 / -1;
  }
}

@media (max-width: 900px) {
  .grid-2,
  .basic-bar {
    grid-template-columns: minmax(0, 1fr);
  }
  .pref-split {
    grid-template-columns: minmax(0, 1fr);
    gap: 12px;
  }
  .pref-nav {
    flex-direction: row;
    flex-wrap: wrap;
  }
  .pref-nav :deep(.el-radio-button) {
    width: auto;
  }
  .pref-pane {
    border-left: none;
    border-top: 1px solid var(--el-border-color-lighter);
    padding-left: 0;
    padding-top: 12px;
  }
  .cat-scroll {
    max-height: 34vh;
  }
}
</style>
