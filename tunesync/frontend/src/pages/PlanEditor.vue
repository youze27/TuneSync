<template>
  <div class="page" v-loading="loading">
    <div class="page-header">
      <div>
        <h2 class="page-title">{{ editing ? `编辑计划 #${editing.id}` : '新建推送计划' }}</h2>
        <div class="page-subtitle">触发 → 内容配方 → 交付，三步配置；右侧实时预览每次执行会发生什么</div>
      </div>
      <div class="toolbar">
        <el-button :icon="ArrowLeft" @click="goBack">返回</el-button>
        <el-button :icon="RefreshLeft" :loading="loading" @click="load">重载</el-button>
        <el-button :icon="Check" :loading="saving && !thenRun" @click="save(false)">保存</el-button>
        <el-button type="primary" :icon="VideoPlay" :loading="saving && thenRun" @click="save(true)">
          保存并立即执行
        </el-button>
      </div>
    </div>

    <el-alert v-if="saveError" type="error" show-icon class="mb-12" @close="saveError = ''">
      <template #title>{{ saveError }}</template>
    </el-alert>

    <el-steps :active="step" finish-status="success" align-center class="mb-12">
      <el-step title="触发" description="何时执行" />
      <el-step title="内容配方" description="产出什么" />
      <el-step title="交付" description="叫什么、写到哪" />
    </el-steps>

    <el-row v-if="draft.plan" :gutter="12">
      <el-col :xs="24" :lg="16">
        <el-card shadow="never">
          <div v-if="step === 0">
            <div class="section-title">基本信息</div>
            <el-form label-position="top">
              <el-form-item label="计划名称">
                <el-input v-model="draft.name" maxlength="64" placeholder="例如：每日热歌推送" />
              </el-form-item>
              <el-form-item label="启用状态">
                <div class="row">
                  <el-switch v-model="draft.plan.trigger.enabled" />
                  <span class="muted">停用后不再按频率触发，但仍可手动「立即执行」</span>
                </div>
              </el-form-item>
            </el-form>
            <div class="section-title">触发规则</div>
            <TriggerEditor v-model="draft.plan.trigger" />
          </div>

          <div v-else-if="step === 1">
            <div class="section-title">歌单来源</div>
            <el-form label-position="top">
              <el-form-item>
                <el-radio-group v-model="draft.plan.recipe.source" class="source-nav">
                  <el-radio-button value="platform">平台歌单拉取</el-radio-button>
                  <el-radio-button value="ai">AI 创作</el-radio-button>
                </el-radio-group>
                <div class="muted tip">
                  {{ isPlatform
                    ? '从音乐平台歌单广场拉取真实歌单，歌单名保持原名；搜索词由所选场景自动决定。'
                    : '歌单与曲目全部由模型生成，不使用平台歌单。' }}
                </div>
              </el-form-item>
            </el-form>

            <div class="section-title">主题与场景</div>
            <div class="toolbar mb-8">
              <el-button size="small" text @click="selectAll(true)">全选</el-button>
              <el-button size="small" text @click="selectAll(false)">全不选</el-button>
              <el-button size="small" text type="warning" @click="invert">反选</el-button>
              <span class="muted">已选 {{ draft.plan.recipe.topic.scenes.length }} 项</span>
            </div>
            <div v-for="g in groupRows" :key="g.key" class="group-block">
              <div class="group-title">
                <span>{{ g.label }}</span>
                <el-button size="small" text @click="toggleGroup(g.key)">全选/反选</el-button>
              </div>
              <div class="cat-group">
                <el-checkbox
                  v-for="c in g.items"
                  :key="c.code"
                  :model-value="draft.plan.recipe.topic.scenes.includes(c.code)"
                  border
                  @change="(on) => toggleOne(c.code, on)"
                >{{ c.name }}</el-checkbox>
              </div>
            </div>

            <el-form label-position="top" class="mt-12">
              <el-form-item label="场景组合">
                <el-radio-group v-model="draft.plan.recipe.topic.combine">
                  <el-radio-button value="separate">单独：每个场景各推一轮</el-radio-button>
                  <el-radio-button value="merge">合并：合成一个综合主题</el-radio-button>
                </el-radio-group>
              </el-form-item>

              <template v-if="isPlatform">
                <el-form-item label="来源平台">
                  <el-checkbox-group :model-value="draft.plan.recipe.platforms" class="cat-group" @change="onPlatforms">
                    <el-checkbox v-for="p in PLATFORMS" :key="p.key" :value="p.key" border>{{ p.name }}</el-checkbox>
                  </el-checkbox-group>
                  <div class="muted tip">
                    「全平台聚合」实际聚合酷狗 / 酷我 / 咪咕；网易云与 QQ 音乐走分类广场，需要单独勾选。同时勾聚合与具体平台会自动收敛为聚合，避免重复拉取。
                  </div>
                </el-form-item>
              </template>

              <el-form-item v-else label="AI 主题指令（可选）">
                <el-input
                  v-model="draft.plan.recipe.instruction"
                  type="textarea"
                  :rows="3"
                  maxlength="2000"
                  show-word-limit
                  placeholder="例如：适合深夜开车听的国风电子，节奏要稳"
                />
                <div class="muted tip">留空则完全按所选场景生成；这条指令只用于 AI 创作，不会拿去搜索。</div>
              </el-form-item>

              <div class="grid-2">
                <el-form-item label="每次执行产出歌单数">
                  <el-input-number v-model="draft.plan.recipe.playlists_per_run" :min="1" :max="50" controls-position="right" />
                  <div class="muted tip">合并模式下固定为 1 张</div>
                </el-form-item>
                <el-form-item v-if="!isPlatform" label="每歌单曲目数">
                  <el-input-number v-model="draft.plan.recipe.tracks_per_playlist" :min="0" :max="200" :step="10" controls-position="right" />
                  <div class="muted tip">0 = 跟随全局设置（{{ globalTracksPer }} 首）</div>
                </el-form-item>
              </div>

              <el-alert v-if="isPlatform" type="success" :closable="false" show-icon
                        title="已推过的歌单会被全局排除，每次拉到的都不重样；歌单名保持源歌单原名，不加日期或平台前缀。" />
            </el-form>
          </div>

          <div v-else>
            <div class="section-title">歌单命名</div>
            <el-form label-position="top">
              <el-form-item label="命名模板">
                <el-input v-model="nameTemplateProxy" class="mono" placeholder="留空 = 使用全局设置（{name}）" />
                <div class="muted tip">
                  可用变量：{category} {name} {platform} {date}。默认保持源歌单原名；这里留空即跟随全局设置。
                </div>
              </el-form-item>
              <el-form-item label="目标端">
                <el-alert type="info" :closable="false" show-icon
                          title="写入所有已启用的推送平台（Navidrome / 飞牛音乐），在「推送平台」页统一配置。" />
              </el-form-item>
            </el-form>

            <div class="section-title">行为说明</div>
            <ul class="notes">
              <li>名字不会被加上日期或平台前缀，时间粒度也不再由「每日」硬编码。</li>
              <li>是否会出现新歌单，由执行频率与「不重样」去重共同决定，而不是靠改名。</li>
              <li>已生成的历史歌单可在「歌单管理」页按生成时间清理，自动清理默认关闭。</li>
            </ul>
          </div>

          <div class="step-bar">
            <el-button :disabled="step === 0" @click="step--">上一步</el-button>
            <el-button v-if="step < 2" type="primary" @click="step++">下一步</el-button>
            <el-button v-else type="primary" :loading="saving" @click="save(false)">保存计划</el-button>
          </div>
        </el-card>
      </el-col>

      <el-col :xs="24" :lg="8">
        <RunPreviewCard :preview="previewData" :loading="previewLoading" :dirty="dirty" :error="previewError" />
      </el-col>
    </el-row>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { ArrowLeft, Check, RefreshLeft, VideoPlay } from '@element-plus/icons-vue'
import * as api from '../api.js'
import TriggerEditor from '../components/TriggerEditor.vue'
import RunPreviewCard from '../components/RunPreviewCard.vue'
import {
  GROUP_LABELS, GROUP_ORDER, boot, ensureSettings, groupedCategories,
  loadDictionary, state,
} from '../store.js'

const route = useRoute()
const router = useRouter()

const PLATFORMS = [
  { key: 'all', name: '全平台聚合' }, { key: 'wy', name: '网易云' },
  { key: 'tx', name: 'QQ 音乐' }, { key: 'kg', name: '酷狗' },
  { key: 'kw', name: '酷我' }, { key: 'mg', name: '咪咕' },
]

const loading = ref(false)
const saving = ref(false)
const thenRun = ref(false)
const saveError = ref('')
const step = ref(0)
const editing = ref(null)
const draft = reactive({ name: '', plan: null })
const savedSnapshot = ref('')
// 直接比较草稿与上次保存的快照，避免 watch 与 load() 的时序竞争把状态误标成「未保存」
const dirty = computed(() => !draft.plan || JSON.stringify(draft) !== savedSnapshot.value)
const previewData = ref(null)
const previewLoading = ref(false)
const previewError = ref('')
let timer = null

const isPlatform = computed(() => draft.plan?.recipe?.source === 'platform')
const globalTracksPer = computed(() => Number(state.settings?.llm?.tracks_per_playlist) || 30)
const groupRows = computed(() =>
  GROUP_ORDER.filter((g) => groupedCategories.value[g]?.length).map((g) => ({
    key: g, label: GROUP_LABELS[g] || g, items: groupedCategories.value[g],
  })),
)

const nameTemplateProxy = computed({
  get: () => draft.plan?.delivery?.name_template ?? '',
  set: (v) => { if (draft.plan) draft.plan.delivery.name_template = v.trim() || null },
})

function fillPlan(p) {
  const base = {
    schema_version: 2,
    trigger: { type: 'daily', time: '07:00', weekdays: [0, 1, 2, 3, 4], monthday: 1,
      interval: 3, anchor_date: new Date().toISOString().slice(0, 10), cron: '',
      misfire: 'skip', grace_minutes: 60, enabled: true },
    recipe: { source: 'platform', topic: { scenes: ['douyin'], combine: 'separate' },
      platforms: ['all'], instruction: '', playlists_per_run: 1,
      tracks_per_playlist: 0, candidate_pool: 120, rotation: 'daily' },
    delivery: { name_template: null, targets: [] },
  }
  const out = JSON.parse(JSON.stringify(base))
  for (const s of ['trigger', 'recipe', 'delivery']) {
    if (p?.[s]) Object.assign(out[s], p[s])
  }
  if (p?.recipe?.topic) Object.assign(out.recipe.topic, p.recipe.topic)
  return out
}

async function load() {
  loading.value = true
  try {
    if (!state.booted) await boot()
    if (!state.categories.length) await loadDictionary().catch(() => {})
    await ensureSettings().catch(() => {})
    const id = Number(route.query.id)
    const plans = await api.getPlans()
    const row = id ? plans.find((p) => p.id === id) : null
    if (id && !row) ElMessage.warning(`计划 #${id} 不存在，已按新建处理`)
    editing.value = row || null
    const plan = row ? row.plan : await api.getDefaultPlan()
    draft.name = row ? row.name : ''
    draft.plan = fillPlan(plan)
    savedSnapshot.value = JSON.stringify(draft)
    refreshPreview()
  } catch (e) {
    api.toastError(e, '加载计划失败')
  } finally {
    loading.value = false
  }
}

async function refreshPreview() {
  if (!draft.plan) return
  previewLoading.value = true
  previewError.value = ''
  try {
    previewData.value = await api.previewPlan(draft.plan)
  } catch (e) {
    previewError.value = api.errText(e, '预览计算失败')
    previewData.value = null
  } finally {
    previewLoading.value = false
  }
}

watch(
  () => draft,
  () => { clearTimeout(timer); timer = setTimeout(refreshPreview, 350) },
  { deep: true },
)

function selectAll(fill) {
  draft.plan.recipe.topic.scenes = fill ? state.categories.map((c) => c.code) : []
}
function invert() {
  const cur = new Set(draft.plan.recipe.topic.scenes)
  draft.plan.recipe.topic.scenes = state.categories.filter((c) => !cur.has(c.code)).map((c) => c.code)
}
function toggleOne(code, on) {
  const set = new Set(draft.plan.recipe.topic.scenes)
  if (on) set.add(code); else set.delete(code)
  draft.plan.recipe.topic.scenes = [...set]
}
function toggleGroup(g) {
  const codes = (groupedCategories.value[g] || []).map((c) => c.code)
  const allIn = codes.every((c) => draft.plan.recipe.topic.scenes.includes(c))
  const set = new Set(draft.plan.recipe.topic.scenes)
  codes.forEach((c) => (allIn ? set.delete(c) : set.add(c)))
  draft.plan.recipe.topic.scenes = [...set]
}
function onPlatforms(list) {
  draft.plan.recipe.platforms = list.includes('all') ? ['all'] : (list.length ? list : ['all'])
}

async function save(runAfter) {
  if (!draft.plan) return
  if (!draft.name.trim()) { ElMessage.warning('先给计划起个名字'); step.value = 0; return }
  if (!draft.plan.recipe.topic.scenes.length) { ElMessage.warning('至少选择一个推送场景'); step.value = 1; return }
  saving.value = true
  thenRun.value = runAfter
  saveError.value = ''
  try {
    const body = { name: draft.name.trim(), plan: draft.plan }
    let id = editing.value?.id
    if (id) await api.putPlan(id, body)
    else {
      const r = await api.createPlan(body)
      id = r.id
      editing.value = { id }
      router.replace({ path: '/push-config', query: { id: String(id) } })
    }
    savedSnapshot.value = JSON.stringify(draft)
    ElMessage.success('计划已保存')
    if (runAfter && id) {
      await api.runPlan(id)
      ElMessage.success('已触发执行')
      router.push('/')
    }
  } catch (e) {
    saveError.value = api.errText(e, '保存失败')
  } finally {
    saving.value = false
    thenRun.value = false
  }
}

function goBack() { router.push('/tasks') }
onMounted(load)
</script>

<style scoped>
.section-title { font-size: 14px; font-weight: 600; margin: 4px 0 12px; }
.mb-8 { margin-bottom: 8px; }
.mb-12 { margin-bottom: 12px; }
.mt-12 { margin-top: 12px; }
.tip { font-size: 12px; line-height: 1.6; color: var(--el-text-color-secondary); }
.muted { color: var(--el-text-color-secondary); font-size: 12px; }
.row { display: flex; align-items: center; gap: 10px; }
.grid-2 { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0 16px; }
.group-block { padding-bottom: 10px; }
.group-title { display: flex; align-items: center; gap: 8px; font-size: 13px; font-weight: 600; margin-bottom: 8px; }
.cat-group { display: flex; flex-wrap: wrap; gap: 8px 10px; }
.cat-group :deep(.el-checkbox) { margin-right: 0; }
.source-nav { margin-bottom: 4px; }
.step-bar { display: flex; justify-content: space-between; gap: 8px; margin-top: 18px; padding-top: 14px; border-top: 1px solid var(--el-border-color-lighter); }
.notes { font-size: 13px; line-height: 1.9; color: var(--el-text-color-regular); padding-left: 20px; }
@media (max-width: 900px) { .grid-2 { grid-template-columns: minmax(0, 1fr); } }
</style>
