<template>
  <div class="page" v-loading="loading">
    <div class="page-header">
      <div>
        <h2 class="page-title">音源管理</h2>
        <div class="page-subtitle">
          音源按优先级从小到大替补使用，连续失败达到阈值会被熔断跳过；按住左侧优先级数字上下拖动即可重排
        </div>
      </div>
      <div class="toolbar">
        <el-button :icon="Refresh" @click="load">刷新</el-button>
        <el-button :icon="CircleCheck" :loading="checkingAll" @click="checkAll">全部检测</el-button>
        <el-button :icon="MagicStick" @click="openQuick">粘贴脚本导入</el-button>
        <el-button :icon="FolderAdd" @click="openBatch">批量导入脚本</el-button>
        <el-button type="primary" :icon="Plus" @click="openCreate">新增音源</el-button>
      </div>
    </div>

    <el-card shadow="never">
      <el-table :data="sources" border stripe v-loading="loading">
        <el-table-column label="优先级" width="112" align="center">
          <!-- 整列就是拖拽手柄：按住上下拖即可重排，落到目标行位置后落库 -->
          <template #default="{ row, $index }">
            <div
              class="prio-cell"
              :class="{ dragging: dragId === row.id, over: dragOverIndex === $index && dragId !== null && dragId !== row.id }"
              draggable="true"
              @dragstart="onDragStart(row, $event)"
              @dragover.prevent="onDragOver($index, $event)"
              @drop.prevent="onDrop($index)"
              @dragend="onDragEnd"
            >
              <el-icon class="prio-grip"><Rank /></el-icon>
              <span class="mono prio-num" :class="{ top: $index === 0 }">{{ row.priority }}</span>
            </div>
          </template>
        </el-table-column>

        <el-table-column prop="name" label="名称" min-width="130" show-overflow-tooltip />

        <el-table-column label="启用" width="80" align="center">
          <template #default="{ row }">
            <el-switch
              :model-value="row.enabled"
              :loading="togglingId === row.id"
              @change="(v) => toggleEnabled(row, v)"
            />
          </template>
        </el-table-column>

        <el-table-column label="健康状态" width="100">
          <template #default="{ row }"><HealthTag :health="row.health" /></template>
        </el-table-column>

        <el-table-column label="失败次数" width="90" align="center">
          <template #default="{ row }">{{ row.fail_count ?? 0 }}</template>
        </el-table-column>

        <el-table-column label="成功率" width="110" align="center">
          <template #default="{ row }">{{ successRateText(row) }}</template>
        </el-table-column>

        <el-table-column label="最近检测" width="130">
          <template #default="{ row }">{{ fmtAgo(row.last_check) }}</template>
        </el-table-column>

        <el-table-column prop="last_error" label="最近错误" min-width="180">
          <template #default="{ row }">
            <el-tooltip
              v-if="row.last_error"
              :content="row.last_error"
              placement="top"
              :show-after="300"
            >
              <span class="err-text err-ellipsis">{{ row.last_error }}</span>
            </el-tooltip>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>

        <el-table-column label="配置预览" min-width="180">
          <template #default="{ row }">
            <el-tooltip placement="top" :show-after="200">
              <template #content>
                <div class="cfg-tip">
                  <div>类型: {{ row.cfg?.kind === 'lx' ? 'LX 音源脚本' : 'HTTP 模板' }}</div>
                  <div>search: {{ row.cfg?.search || '—' }}</div>
                  <template v-if="row.cfg?.kind === 'lx'">
                    <div>platform: {{ row.cfg?.platform || 'wy' }}</div>
                    <div>script: {{ (row.cfg?.script || '').slice(0, 60) || '—' }}</div>
                  </template>
                  <template v-else>
                    <div>url: {{ row.cfg?.url || '—' }}</div>
                    <div>list_path: {{ row.cfg?.list_path || '—' }}</div>
                    <div>quality: {{ row.cfg?.quality || '—' }}</div>
                  </template>
                </div>
              </template>
              <span class="mono muted cfg-preview">{{ row.cfg?.search || '未配置' }}</span>
            </el-tooltip>
          </template>
        </el-table-column>

        <el-table-column label="操作" width="190" fixed="right">
          <template #default="{ row }">
            <el-button size="small" type="primary" text :loading="checkingId === row.id" @click="checkOne(row)">
              检测
            </el-button>
            <el-button size="small" text @click="openEdit(row)">编辑</el-button>
            <el-button size="small" type="danger" text @click="remove(row)">删除</el-button>
          </template>
        </el-table-column>

        <template #empty>
          <div class="empty-hint">还没有配置任何音源，点击「新增音源」添加。</div>
        </template>
      </el-table>
    </el-card>

    <!-- 新增 / 编辑 -->
    <el-dialog v-model="dialogVisible" :title="editing ? `编辑音源 #${editing.id}` : '新增音源'" width="760px" top="6vh" destroy-on-close>
      <el-form ref="formRef" :model="meta" :rules="rules" label-width="92px">
        <el-form-item label="音源名称" prop="name">
          <el-input v-model="meta.name" maxlength="64" show-word-limit placeholder="例如：主音源" />
        </el-form-item>
        <el-form-item label="音源类型">
          <el-radio-group v-model="meta.cfg.kind">
            <el-radio-button value="http">HTTP 模板</el-radio-button>
            <el-radio-button value="lx">LX 音源脚本</el-radio-button>
          </el-radio-group>
          <span class="muted" style="margin-left: 10px; font-size: 12px">
            {{ meta.cfg.kind === 'lx' ? '搜索走下方模板，取链接交给脚本' : '搜索与取链接都是 URL 模板' }}
          </span>
        </el-form-item>
        <el-form-item label="优先级" prop="priority">
          <el-input-number v-model="meta.priority" :min="1" :max="999" />
          <span class="muted" style="margin-left: 8px; font-size: 12px">数字越小越先用</span>
        </el-form-item>
        <el-form-item label="启用">
          <el-switch v-model="meta.enabled" />
        </el-form-item>
      </el-form>

      <el-divider content-position="left">
        接口配置（{{ meta.cfg.kind === 'lx' ? '搜索模板 + LX 脚本' : 'http-api 通用模板' }}）
      </el-divider>
      <SourceForm :cfg="meta.cfg" :kind="meta.cfg.kind" />

      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="submit">保存</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="quickVisible" title="粘贴 LX 音源脚本导入" width="640px" top="8vh" destroy-on-close>
      <el-alert type="info" :closable="false" show-icon class="quick-tip"
                title="与落雪音乐相同：粘贴音源脚本的链接或完整文本，自动识别名称与平台导入。链接方式会跟随上游更新（10 分钟缓存）。" />
      <el-input v-model="quickForm.script" type="textarea" :rows="10"
                placeholder="两种形式均可：&#10;1) 脚本链接，例如 https://ghproxy.net/raw.githubusercontent.com/pdone/lx-music-source/main/changqing/latest.js&#10;2) 直接粘贴完整脚本文本（含 @name 头注释）" />
      <div class="quick-row">
        <span class="quick-label">取链平台</span>
        <el-select v-model="quickForm.platform" style="width: 240px">
          <el-option label="自动识别（默认网易云）" value="" />
          <el-option label="网易云 (wy)" value="wy" />
          <el-option label="酷狗 (kg)" value="kg" />
          <el-option label="酷我 (kw)" value="kw" />
          <el-option label="QQ 音乐 (tx)" value="tx" />
          <el-option label="咪咕 (mg)" value="mg" />
        </el-select>
      </div>
      <template #footer>
        <el-button @click="quickVisible = false">取消</el-button>
        <el-button type="primary" :loading="quickSaving" @click="doQuickImport">识别并导入</el-button>
      </template>
    </el-dialog>

    <!-- 批量导入：拖入多个落雪 js/txt 文件，或粘贴多个脚本链接（一行一个） -->
    <el-dialog v-model="batchVisible" title="批量导入落雪脚本" width="680px" top="6vh" destroy-on-close>
      <el-alert type="info" :closable="false" show-icon class="quick-tip"
                title="一次导入多个 LX 音源：拖入多个脚本文件，或粘贴多个脚本链接；逐个沙箱预检后建源。" />
      <el-upload
        v-model:file-list="batchFiles"
        drag
        multiple
        accept=".js,.txt"
        :auto-upload="false"
      >
        <el-icon :size="36" style="color: var(--el-text-color-placeholder)"><FolderAdd /></el-icon>
        <div class="el-upload__text">把脚本文件拖到这里，或 <em>点击选择</em>（.js / .txt，可多选）</div>
      </el-upload>
      <el-input
        v-model="batchUrls"
        type="textarea"
        :rows="5"
        class="mt-12"
        placeholder="脚本链接：一行一个，例如 https://ghproxy.net/raw.githubusercontent.com/pdone/lx-music-source/main/changqing/latest.js"
      />
      <div class="quick-row">
        <span class="quick-label">取链平台</span>
        <el-select v-model="batchPlatform" style="width: 240px">
          <el-option label="自动识别（默认网易云）" value="" />
          <el-option label="网易云 (wy)" value="wy" />
          <el-option label="酷狗 (kg)" value="kg" />
          <el-option label="酷我 (kw)" value="kw" />
          <el-option label="QQ 音乐 (tx)" value="tx" />
          <el-option label="咪咕 (mg)" value="mg" />
        </el-select>
      </div>
      <template #footer>
        <el-button @click="batchVisible = false">取消</el-button>
        <el-button type="primary" :loading="batchSaving" @click="doBatchImport">开始导入</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox, ElNotification } from 'element-plus'
import { CircleCheck, FolderAdd, MagicStick, Plus, Rank, Refresh } from '@element-plus/icons-vue'
import * as api from '../api.js'
import HealthTag from '../components/HealthTag.vue'
import SourceForm from '../components/SourceForm.vue'
import { fmtAgo } from '../utils.js'

const loading = ref(false)
const saving = ref(false)
const checkingId = ref(null)
const checkingAll = ref(false)
const togglingId = ref(null)
const moving = ref(false)
const dragId = ref(null)
const dragOverIndex = ref(-1)
const dialogVisible = ref(false)
const editing = ref(null)
const formRef = ref(null)

const list = ref([])
/** 累计成功率：ok+err 都为 0 视为未知（返回 null，排最后）。 */
function successRate(row) {
  const ok = Number(row.ok_count) || 0
  const err = Number(row.err_count) || 0
  if (!ok && !err) return null
  return ok / (ok + err)
}

function successRateText(row) {
  const ok = Number(row.ok_count) || 0
  const err = Number(row.err_count) || 0
  if (!ok && !err) return '—'
  return `${Math.round((ok / (ok + err)) * 100)}% (${ok}/${err})`
}

/** 默认按成功率从高到低展示，未知（还没下载过）的排最后。 */
const sources = computed(() =>
  [...list.value].sort((a, b) => {
    const ra = successRate(a)
    const rb = successRate(b)
    if (ra === null && rb === null) return (a.priority ?? 0) - (b.priority ?? 0)
    if (ra === null) return 1
    if (rb === null) return -1
    return rb - ra
  }),
)

// ── 落雪式快速导入：粘贴脚本链接或全文，后端沙箱预检后自动建源 ──
const quickVisible = ref(false)
const quickSaving = ref(false)
const quickForm = reactive({ script: '', platform: '' })

function openQuick() {
  quickForm.script = ''
  quickForm.platform = ''
  quickVisible.value = true
}

async function doQuickImport() {
  if (!quickForm.script.trim()) {
    ElMessage.warning('请粘贴脚本文本或脚本链接')
    return
  }
  quickSaving.value = true
  try {
    const out = await api.quickImportLx({ script: quickForm.script, platform: quickForm.platform })
    ElMessage.success(`已导入「${out.name}」（platform=${out.platform}），正在探活…`)
    quickVisible.value = false
    await load()
    if (out.id) {
      api.checkSource(out.id).then(() => load()).catch(() => {})
    }
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '导入失败')
  } finally {
    quickSaving.value = false
  }
}

// ── 批量导入：多个脚本文件 + 多个链接，一次请求逐个预检建源 ──
const batchVisible = ref(false)
const batchSaving = ref(false)
const batchFiles = ref([])
const batchUrls = ref('')
const batchPlatform = ref('')

function openBatch() {
  batchFiles.value = []
  batchUrls.value = ''
  batchPlatform.value = ''
  batchVisible.value = true
}

async function doBatchImport() {
  const scripts = []
  for (const f of batchFiles.value) {
    if (!f.raw) continue
    try {
      const text = await f.raw.text()
      if (text.trim()) scripts.push(text)
    } catch {
      ElMessage.warning(`读取文件「${f.name}」失败，已跳过`)
    }
  }
  const urls = (batchUrls.value || '')
    .split('\n')
    .map((s) => s.trim())
    .filter(Boolean)
  scripts.push(...urls)
  if (!scripts.length) {
    ElMessage.warning('请先选择脚本文件或粘贴脚本链接')
    return
  }
  batchSaving.value = true
  try {
    const r = await api.quickImportLxBatch(scripts, batchPlatform.value)
    const fails = (r.results || []).filter((x) => !x.ok)
    const failText = fails.map((x) => `${x.script || '（空脚本）'}：${x.error || '未知原因'}`).join('\n')
    ElNotification({
      title: `批量导入完成：成功 ${r.ok ?? 0} 个，失败 ${r.failed ?? 0} 个`,
      message: failText || '全部导入成功',
      type: fails.length ? 'warning' : 'success',
      duration: fails.length ? 0 : 4500,
    })
    batchVisible.value = false
    await load()
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '批量导入失败')
  } finally {
    batchSaving.value = false
  }
}

const defaultCfg = () => ({
  kind: 'http',
  search: '',
  list_path: 'data.songs',
  id_field: 'id',
  title_field: 'name',
  artist_field: 'artist',
  album_field: 'album',
  url: '',
  url_paths: ['data.url'],
  quality: 'flac',
  platform: 'wy',
  script: '',
})

const meta = reactive({
  name: '',
  priority: 1,
  enabled: true,
  cfg: defaultCfg(),
})

const rules = {
  name: [{ required: true, message: '请填写音源名称', trigger: 'blur' }],
  priority: [{ type: 'number', required: true, message: '请填写优先级', trigger: 'change' }],
}

async function load() {
  loading.value = true
  try {
    list.value = ((await api.getSources()) || []).slice().sort((a, b) => (a.priority ?? 0) - (b.priority ?? 0))
  } catch (e) {
    api.toastError(e, '加载音源失败')
  } finally {
    loading.value = false
  }
}

function openCreate() {
  editing.value = null
  meta.name = ''
  meta.priority = (list.value.length || 0) + 1
  meta.enabled = true
  meta.cfg = defaultCfg()
  dialogVisible.value = true
}

function openEdit(row) {
  editing.value = row
  meta.name = row.name
  meta.priority = row.priority
  meta.enabled = row.enabled
  meta.cfg = { ...defaultCfg(), ...(row.cfg || {}) }
  if (!Array.isArray(meta.cfg.url_paths) || !meta.cfg.url_paths.length) meta.cfg.url_paths = ['data.url']
  dialogVisible.value = true
}

async function submit() {
  const ok = await formRef.value?.validate().catch(() => false)
  if (!ok) return
  const isLx = meta.cfg.kind === 'lx'
  if (!meta.cfg.search) {
    ElMessage.warning('搜索 URL 为必填项')
    return
  }
  if (isLx && !(meta.cfg.script || '').trim()) {
    ElMessage.warning('LX 音源需要脚本：粘贴脚本文本或填脚本 URL')
    return
  }
  if (!isLx) {
    if (!meta.cfg.url) {
      ElMessage.warning('搜索 URL 与取链 URL 为必填项')
      return
    }
    const cleanPaths = (meta.cfg.url_paths || []).map((p) => String(p || '').trim()).filter(Boolean)
    if (!cleanPaths.length) {
      ElMessage.warning('至少保留一条链接取值路径')
      return
    }
    meta.cfg.url_paths = cleanPaths
  }
  const body = {
    name: meta.name,
    priority: meta.priority,
    enabled: meta.enabled,
    cfg: { ...meta.cfg },
  }
  saving.value = true
  try {
    if (editing.value) {
      await api.updateSource(editing.value.id, body)
      ElMessage.success('音源已更新')
    } else {
      await api.createSource(body)
      ElMessage.success('音源已创建')
    }
    dialogVisible.value = false
    await load()
  } catch (e) {
    api.toastError(e, '保存失败')
  } finally {
    saving.value = false
  }
}

/** 按住优先级手柄拖动：记录被拖行、经过的行，松手时按新顺序落库。 */
function onDragStart(row, ev) {
  if (moving.value) {
    ev.preventDefault()
    return
  }
  dragId.value = row.id
  ev.dataTransfer.effectAllowed = 'move'
  // Firefox 必须 setData 才会真正启动拖拽
  ev.dataTransfer.setData('text/plain', String(row.id))
}

function onDragOver(index, ev) {
  if (dragId.value === null || moving.value) return
  ev.dataTransfer.dropEffect = 'move'
  dragOverIndex.value = index
}

async function onDrop(index) {
  const from = dragId.value === null ? -1 : sources.value.findIndex((s) => s.id === dragId.value)
  onDragEnd()
  if (from < 0 || index < 0 || from === index) return
  const next = [...sources.value]
  const [item] = next.splice(from, 1)
  next.splice(index, 0, item)
  await persistOrder(next)
}

function onDragEnd() {
  dragId.value = null
  dragOverIndex.value = -1
}

/** 把重排后的顺序落库：按位置重编 1..N，只 PUT 优先级真的变了的那几行。 */
async function persistOrder(next) {
  moving.value = true
  try {
    for (let i = 0; i < next.length; i += 1) {
      const row = next[i]
      if (row.priority === i + 1) continue
      await api.updateSource(row.id, {
        name: row.name,
        priority: i + 1,
        enabled: row.enabled,
        cfg: row.cfg,
      })
      row.priority = i + 1
    }
    list.value = next
    ElMessage.success('优先级已更新')
  } catch (e) {
    api.toastError(e, '调整优先级失败')
  } finally {
    moving.value = false
    load()
  }
}

async function toggleEnabled(row, val) {
  togglingId.value = row.id
  try {
    await api.updateSource(row.id, {
      name: row.name,
      priority: row.priority,
      enabled: val,
      cfg: row.cfg,
    })
    row.enabled = val
    ElMessage.success(val ? '音源已启用' : '音源已停用')
  } catch (e) {
    api.toastError(e, '更新失败')
  } finally {
    togglingId.value = null
  }
}

async function checkOne(row) {
  checkingId.value = row.id
  try {
    const r = await api.checkSource(row.id)
    if (r?.ok) {
      ElMessage.success(`「${row.name}」检测通过`)
    } else {
      ElMessage.warning(`「${row.name}」检测失败：${r?.error || '未知原因'}`)
    }
    await load()
  } catch (e) {
    api.toastError(e, '检测失败')
  } finally {
    checkingId.value = null
  }
}

async function checkAll() {
  checkingAll.value = true
  try {
    const r = await api.checkAllSources()
    ElMessage.success(r?.detail || '已在后台检测全部音源')
    setTimeout(load, 3000)
  } catch (e) {
    api.toastError(e, '检测失败')
  } finally {
    checkingAll.value = false
  }
}

async function remove(row) {
  try {
    await ElMessageBox.confirm(
      `确定删除音源「${row.name}」吗？删除后该音源将不再参与下载替补。`,
      '删除确认',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消', confirmButtonClass: 'el-button--danger' },
    )
  } catch {
    return
  }
  try {
    await api.deleteSource(row.id)
    ElMessage.success('音源已删除')
    await load()
  } catch (e) {
    api.toastError(e, '删除失败')
  }
}

onMounted(load)
</script>

<style scoped>
.prio-cell {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 4px;
  min-width: 56px;
  padding: 3px 8px;
  border-radius: 6px;
  cursor: grab;
  user-select: none;
  transition: background-color 0.15s, box-shadow 0.15s;
}
.prio-cell:hover {
  background-color: var(--el-fill-color-light);
}
.prio-cell.dragging {
  cursor: grabbing;
  opacity: 0.45;
}
/* 拖动经过的目标行，给一条插入位置的提示线 */
.prio-cell.over {
  background-color: var(--el-color-primary-light-9);
  box-shadow: inset 0 0 0 1px var(--el-color-primary-light-5);
}
.prio-grip {
  color: var(--el-text-color-placeholder);
  font-size: 14px;
}
.prio-cell:hover .prio-grip {
  color: var(--el-text-color-regular);
}
.prio-num {
  min-width: 18px;
  text-align: center;
  font-weight: 600;
}
/* 优先级 1 就是首选音源，标红一眼能认出来 */
.prio-num.top {
  color: var(--el-color-danger);
}
.cfg-preview {
  display: inline-block;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  vertical-align: bottom;
}
.cfg-tip {
  font-size: 12px;
  line-height: 1.7;
}
.err-text {
  color: #f56c6c;
  font-size: 12px;
}
.err-ellipsis {
  display: block;
  max-width: 220px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.quick-tip {
  margin-bottom: 12px;
}
.quick-row {
  margin-top: 10px;
  display: flex;
  align-items: center;
  gap: 10px;
}
.quick-label {
  color: #606266;
  font-size: 13px;
}
</style>