<template>
  <div class="page" v-loading="loading">
    <div class="page-header">
      <div>
        <h2 class="page-title">全局设置</h2>
        <div class="page-subtitle">
          整站通用的配置：模型、歌单命名、下载参数、访问安全；改完立即生效，无需重启
        </div>
      </div>
      <div class="toolbar">
        <el-button :icon="RefreshLeft" @click="resetForm">恢复已保存值</el-button>
        <el-button type="primary" :icon="Check" :loading="saving" @click="save">保存</el-button>
      </div>
    </div>

    <el-alert type="info" show-icon :closable="false" class="mb-12">
      <template #title>
        这里的配置存在服务端（<code>PUT /api/settings</code>），换设备、换人打开都能看到同一份设置；
        每条推送计划的场景与频率请到<router-link to="/push-config">新建计划</router-link>里改。
      </template>
    </el-alert>

    <el-alert v-if="saveError" type="error" show-icon class="mb-12" @close="saveError = ''">
      <template #title>{{ saveError }}</template>
    </el-alert>

    <el-row :gutter="12">
      <el-col :xs="24" :lg="12">
        <el-card shadow="never" class="mb-12">
          <template #header>
            <div class="card-head">
              <span class="card-title"><el-icon><ChatDotRound /></el-icon> LLM 歌单生成</span>
              <el-tag v-if="llmConfigured" size="small" type="success" effect="plain">已配置</el-tag>
              <el-tag v-else size="small" type="warning" effect="plain">未配置</el-tag>
            </div>
          </template>

          <el-form label-width="108px" label-position="left">
            <el-form-item label="接口地址">
              <el-input
                v-model="form.llm.base_url"
                placeholder="https://api.deepseek.com/v1"
                class="mono"
              />
              <div class="muted tip">
                任意 OpenAI 兼容的 <code>/chat/completions</code> 端点（DeepSeek / 通义 / Kimi / 本地 vLLM 等）
              </div>
            </el-form-item>

            <el-form-item label="API Key">
              <el-input
                v-model="form.llm.api_key"
                type="password"
                show-password
                autocomplete="new-password"
                placeholder="不需要鉴权就留空"
              />
              <div class="muted tip">
                已保存的 Key 回显为 <code>***</code>；留空或保持 <code>***</code> 都表示不修改；
                换新 Key 直接粘贴覆盖，无需先清空
              </div>
            </el-form-item>

            <el-form-item label="模型">
              <el-input v-model="form.llm.model" placeholder="deepseek-chat" class="mono" />
            </el-form-item>

            <el-form-item label="温度">
              <el-input-number v-model="form.llm.temperature" :min="0" :max="2" :step="0.1" />
              <span class="muted" style="margin-left: 8px; font-size: 12px">越高选曲越多样，越低越稳定</span>
            </el-form-item>

            <el-form-item label="每张曲目数">
              <el-input-number v-model="form.llm.tracks_per_playlist" :min="1" :max="100" />
              <span class="muted" style="margin-left: 8px; font-size: 12px">
                全局默认值，单条计划可在推送配置里单独覆盖
              </span>
            </el-form-item>

            <el-form-item label="候选送审上限">
              <el-input-number v-model="form.llm.select_pool" :min="5" :max="120" />
              <span class="muted" style="margin-left: 8px; font-size: 12px">
                「乐源拉取 + LLM 筛选」时一次给模型过目的候选歌单数
              </span>
            </el-form-item>

            <el-form-item label="连通性">
              <el-button :icon="Connection" :loading="llmTesting" @click="doTestLlm">测试连通</el-button>
              <span class="muted" style="margin-left: 10px; font-size: 12px">
                按当前表单内容发一次真实请求——新 Key 不用先保存，测通再存
              </span>
            </el-form-item>
          </el-form>
        </el-card>
      </el-col>

      <el-col :xs="24" :lg="12">
        <el-card shadow="never">
          <template #header>
            <div class="card-head">
              <span class="card-title"><el-icon><Lock /></el-icon> 访问安全</span>
              <el-tag v-if="auth.enabled" size="small" type="success" effect="plain">已开启登录校验</el-tag>
              <el-tag v-else size="small" type="danger" effect="plain">未开启登录校验</el-tag>
            </div>
          </template>

          <el-form label-width="108px" label-position="left">
            <el-form-item label="登录校验">
              <el-switch v-model="form.auth.enabled" />
              <span class="muted" style="margin-left: 10px; font-size: 12px">
                关闭后任何人打开页面都能直接操作，请只在可信网络里临时使用
              </span>
            </el-form-item>

            <el-form-item label="SSO 身份头">
              <el-input
                v-model="form.auth.proxy_header"
                placeholder="留空即用本地账号登录"
                class="mono"
                :disabled="!form.auth.enabled"
              />
              <div class="muted tip">
                接 SSO（雷池等反代）时填反代注入的可信身份请求头，例如 <code>X-Remote-User</code>；
                填了该头的请求直接视为已登录。<strong>只有前面确实挂了会校验身份、并会清掉外部同名头的反代才能填</strong>。
              </div>
            </el-form-item>

            <el-form-item label="已有账号">
              <el-tag v-if="authInfo.users" type="info" effect="plain">
                {{ authInfo.users }} 个
              </el-tag>
              <span v-else class="muted">还没有账号</span>
              <span class="muted" style="margin-left: 8px; font-size: 12px">
                口令用右上角用户菜单里的「修改口令」来改
              </span>
            </el-form-item>

            <el-alert v-if="!authInfo.users" type="warning" show-icon :closable="false">
              <template #title>还没有任何账号：现在退出登录就无法再登录了，请先到登录页初始化管理员</template>
            </el-alert>
          </el-form>
        </el-card>
      </el-col>
      <el-col :xs="24" :lg="12">
        <el-card shadow="never" class="mb-12">
          <template #header>
            <span class="card-title"><el-icon><Tickets /></el-icon> 歌单命名</span>
          </template>

          <el-form label-width="108px" label-position="left">
            <el-form-item label="命名模板">
              <el-input v-model="form.naming.playlist" placeholder="{name}" class="mono" />
              <div class="muted tip">
                可用变量：
                <el-tag
                  v-for="v in NAMING_VARS"
                  :key="v"
                  size="small"
                  effect="plain"
                  class="mono"
                >
                  {{ v }}
                </el-tag>
              </div>
            </el-form-item>

            <el-form-item label="命名预览">
              <el-input :model-value="namingPreview" disabled />
            </el-form-item>
          </el-form>
        </el-card>

        <el-card shadow="never">
          <template #header>
            <span class="card-title"><el-icon><Download /></el-icon> 下载设置</span>
          </template>

          <el-form label-width="108px" label-position="left">
            <div class="grid-2">
              <el-form-item label="并发数">
                <el-input-number
                  v-model="form.download.concurrency"
                  :min="1"
                  :max="16"
                  controls-position="right"
                  style="width: 100%"
                />
                <div class="muted tip">同时下载的歌曲数</div>
              </el-form-item>

              <el-form-item label="首选音质">
                <el-select v-model="form.download.quality" style="width: 100%">
                  <el-option v-for="q in QUALITIES" :key="q" :label="q" :value="q" />
                </el-select>
                <div class="muted tip">FLAC 覆盖少，多数歌会先失败一次再降级</div>
              </el-form-item>
            </div>

            <el-form-item label="音质降级链">
              <el-select
                v-model="form.download.quality_fallback"
                multiple
                allow-create
                filterable
                default-first-option
                style="width: 100%"
              >
                <el-option v-for="q in QUALITIES" :key="q" :label="q" :value="q" />
              </el-select>
              <div class="muted tip">首选音质取不到时，按顺序依次重试</div>
            </el-form-item>

            <div class="grid-2">
              <el-form-item label="时长下限">
                <el-input-number
                  v-model="form.download.min_duration_sec"
                  :min="0"
                  :max="600"
                  controls-position="right"
                  style="width: 100%"
                />
                <div class="muted tip">秒，低于视为无效音频</div>
              </el-form-item>

              <el-form-item label="体积下限">
                <el-input-number
                  v-model="form.download.min_bytes"
                  :min="0"
                  :max="100000000"
                  :step="100000"
                  controls-position="right"
                  style="width: 100%"
                />
                <div class="muted tip">字节，定得太大会误杀低码率音源</div>
              </el-form-item>
            </div>

            <div class="muted tip">
              默认值偏宽松：只拦切片和试听片段，宁可多下也不把歌整首丢掉。
              想拿无损把首选音质改成 flac 即可，失败仍会按降级链重试。
            </div>
          </el-form>
        </el-card>
      </el-col>
    </el-row>

    <el-card shadow="never" class="mt-12">
      <template #header>
        <div class="card-head">
          <span class="card-title"><el-icon><FolderOpened /></el-icon> 配置导入导出</span>
        </div>
      </template>

      <div class="cfg-row">
        <el-button :icon="Download" :loading="exporting" @click="doExport">导出配置</el-button>
        <el-upload
          ref="uploadRef"
          accept=".json"
          :auto-upload="false"
          :limit="1"
          :show-file-list="false"
          :on-change="onImportFile"
        >
          <el-button type="primary" plain :icon="Upload" :loading="importing">导入配置</el-button>
        </el-upload>
      </div>
      <div class="muted tip">
        导出内容包含全部计划任务与设置，可用于迁移到另一台机器；
        推送口令与 LLM 密钥不包含在导出中，导入后需在目标库 / 本页重新填写。
      </div>
    </el-card>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { ChatDotRound, Check, Connection, Download, FolderOpened, Lock, RefreshLeft, Tickets, Upload } from '@element-plus/icons-vue'
import * as api from '../api.js'
import { auth, boot, ensureSettings, invalidateSettings, state } from '../store.js'

const NAMING_VARS = ['{category}', '{name}', '{platform}']
const QUALITIES = ['flac', '320', '128', '96']

const loading = ref(false)
const saving = ref(false)
const saveError = ref('')

/** 默认值与 config.yaml 保持一致：偏向「先把歌弄下来」，校验别卡太死。 */
const form = reactive({
  llm: { base_url: '', api_key: '', model: '', temperature: 0.8, tracks_per_playlist: 30, select_pool: 40 },
  naming: { playlist: '{name}' },
  download: {
    concurrency: 5,
    quality: '320',
    quality_fallback: ['128', '96'],
    min_duration_sec: 10,
    min_bytes: 300000,
  },
  auth: { enabled: true, proxy_header: '' },
})

const authInfo = computed(() => state.settings?.auth || {})

/** base_url 与 model 都填了才算配置完成，否则推送时歌单生成必然失败。 */
const llmConfigured = computed(
  () => Boolean((form.llm.base_url || '').trim()) && Boolean((form.llm.model || '').trim()),
)

const namingPreview = computed(() =>
  (form.naming.playlist || '')
    .replaceAll('{category}', '自定义')
    .replaceAll('{name}', '深夜循环·抖音热歌精选')
    .replaceAll('{platform}', 'LLM'),
)

function fillFromSettings(s) {
  state.settings = s || {}

  const l = s?.llm || {}
  form.llm = {
    base_url: l.base_url ?? '',
    api_key: l.api_key ?? '',
    model: l.model ?? '',
    temperature: Number.isFinite(Number(l.temperature)) ? Number(l.temperature) : 0.8,
    tracks_per_playlist: Number.isInteger(l.tracks_per_playlist) ? l.tracks_per_playlist : 30,
    select_pool: Number.isInteger(l.select_pool) ? l.select_pool : 40,
  }
  form.naming = { playlist: s?.naming?.playlist || '{name}' }

  const d = s?.download || {}
  form.download = {
    concurrency: d.concurrency ?? 5,
    quality: d.quality ?? '320',
    quality_fallback: Array.isArray(d.quality_fallback) && d.quality_fallback.length
      ? [...d.quality_fallback]
      : ['128', '96'],
    min_duration_sec: d.min_duration_sec ?? 10,
    min_bytes: d.min_bytes ?? 300000,
  }

  const a = s?.auth || {}
  form.auth = {
    enabled: a.enabled !== false,
    proxy_header: a.proxy_header || '',
  }
}

/** force=true 时绕过缓存重新拉取（「恢复已保存值」按钮用）。 */
async function load(force = false) {
  loading.value = true
  try {
    fillFromSettings(await ensureSettings(force))
    state.error = ''
  } catch (e) {
    api.toastError(e, '加载设置失败')
  } finally {
    loading.value = false
  }
}

async function save() {
  saving.value = true
  saveError.value = ''
  try {
    const body = {
      llm: { ...form.llm },
      naming: { playlist: form.naming.playlist },
      download: { ...form.download },
      auth: { enabled: form.auth.enabled, proxy_header: (form.auth.proxy_header || '').trim() },
    }
    // PUT 直接返回合并后的 settings，用它回填可以让掩码/归一化结果立刻可见
    // 保存前记下 Key 框内容：提交后会被掩码 *** 回填，能不能算「更新了 Key」只有现在知道
    const keyUpdated = Boolean(form.llm.api_key && form.llm.api_key !== '***')
    const res = await api.putSettings(body)
    // 全局缓存里还存着保存前那份旧值，不清的话回到设置页看到的还是改之前的值
    invalidateSettings()
    fillFromSettings(res)
    ElMessage.success(keyUpdated ? '全局设置已保存，API Key 已更新' : '全局设置已保存（API Key 保持不变）')
  } catch (e) {
    // 失败时保留用户当前输入，并把后端 detail 就地显示出来而不是只弹一下就消失
    saveError.value = api.errText(e, '保存失败')
  } finally {
    saving.value = false
  }
}

const resetForm = () => {
  load(true)
  ElMessage.info('已恢复为后端当前生效值')
}

// ───────────────────────────── LLM 连通性测试

const llmTesting = ref(false)

async function doTestLlm() {
  llmTesting.value = true
  try {
    // 测的是表单当前值而不是已保存值：新 Key 粘贴后可直接测，不用先存
    const ov = {
      base_url: form.llm.base_url,
      model: form.llm.model,
      temperature: form.llm.temperature,
    }
    if (form.llm.api_key && form.llm.api_key !== '***') ov.api_key = form.llm.api_key
    const r = await api.testLlm(ov)
    ElMessage.success(`连通正常 · 模型 ${r.model || '—'} · 延迟 ${r.latency_ms ?? '—'} ms · 回复「${r.reply || ''}」`)
  } catch (e) {
    ElMessage.error(api.errText(e, 'LLM 连通测试失败'))
  } finally {
    llmTesting.value = false
  }
}

// ───────────────────────────── 配置导入导出

const exporting = ref(false)
const importing = ref(false)
const uploadRef = ref(null)

function pad(n) {
  return String(n).padStart(2, '0')
}

async function doExport() {
  exporting.value = true
  try {
    const data = await api.exportConfig()
    const d = new Date()
    const name = `tunesync-config-${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}.json`
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = name
    a.click()
    URL.revokeObjectURL(url)
    ElMessage.success(`已导出 ${name}`)
  } catch (e) {
    api.toastError(e, '导出配置失败')
  } finally {
    exporting.value = false
  }
}

async function onImportFile(file) {
  // 选完即清，下一次再选同一个文件也会触发 on-change
  uploadRef.value?.clearFiles()
  let data
  try {
    const text = await file.raw.text()
    data = JSON.parse(text)
  } catch {
    ElMessage.error('文件不是有效的 JSON，请使用导出的配置文件')
    return
  }
  const taskCount = Array.isArray(data?.tasks) ? data.tasks.length : 0
  const settingsCount = Object.keys(data?.settings || {}).length
  try {
    await ElMessageBox.confirm(
      `将导入 ${taskCount} 个计划任务与 ${settingsCount} 节配置，同名任务会被更新。确定继续？`,
      '导入确认',
      { type: 'warning', confirmButtonText: '导入', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  importing.value = true
  try {
    const r = await api.importConfig(data)
    ElMessage.success(`导入完成：新建 ${r.tasks_new ?? 0} 个任务，更新 ${r.tasks_updated ?? 0} 个任务`)
    ElMessage.info('推送口令与 LLM 密钥不包含在导出中，需在目标库/设置页重新填写')
  } catch (e) {
    api.toastError(e, '导入配置失败')
  } finally {
    importing.value = false
  }
}

onMounted(async () => {
  if (!state.booted) await boot()
  await load()
})
</script>

<style scoped>
.card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}
.card-title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-weight: 600;
  font-size: 14px;
}
.tip {
  font-size: 12px;
  margin-top: 4px;
}
.cfg-row {
  display: flex;
  align-items: center;
  gap: 12px;
}
</style>
