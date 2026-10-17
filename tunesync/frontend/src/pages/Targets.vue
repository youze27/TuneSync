<template>
  <div class="page" v-loading="loading">
    <div class="page-header">
      <div>
        <h2 class="page-title">推送平台</h2>
        <div class="page-subtitle">下载完成的歌单写入飞牛音乐与 Navidrome</div>
      </div>
      <div class="toolbar">
        <el-button :icon="Refresh" @click="load">刷新</el-button>
        <el-button type="primary" :icon="Check" :loading="saving" @click="save">保存</el-button>
      </div>
    </div>

    <el-alert type="info" show-icon :closable="false" class="mb-12">
      <template #title>
        密码框留空表示<strong>不修改</strong>该密码：后端把 <code>***</code> 和空串都当作「保持原值」，
        所以这里显示的永远是掩码而不是真实口令。关闭「参与自动同步」后配置仍然保留，也可以随时测试连接。
      </template>
    </el-alert>

    <el-row :gutter="12">
      <el-col :xs="24" :lg="12">
        <el-card shadow="never" class="mb-12">
          <template #header>
            <div class="card-head">
              <span class="card-title"><el-icon><Headset /></el-icon> Navidrome</span>
              <div class="toolbar">
                <el-tag v-if="!form.navidrome.enabled" size="small" type="warning" effect="plain">
                  未参与自动同步
                </el-tag>
                <el-tag size="small" :type="testResult.navidrome?.ok ? 'success' : testResult.navidrome ? 'danger' : 'info'">
                  {{ testLabel(testResult.navidrome) }}
                </el-tag>
              </div>
            </div>
          </template>

          <el-form label-width="96px" label-position="left">
            <el-form-item label="参与自动同步">
              <el-switch v-model="form.navidrome.enabled" />
              <span class="muted" style="margin-left: 10px; font-size: 12px">
                关闭后不再自动同步，仍可测试连接
              </span>
            </el-form-item>
            <el-form-item label="服务地址">
              <el-input v-model="form.navidrome.url" placeholder="http://192.168.1.100:4533" />
            </el-form-item>
            <el-form-item label="用户名">
              <el-input v-model="form.navidrome.user" placeholder="admin" />
            </el-form-item>
            <el-form-item label="密码">
              <el-input
                v-model="form.navidrome.password"
                type="password"
                show-password
                placeholder="留空则不修改"
                autocomplete="new-password"
              />
            </el-form-item>
          </el-form>

          <div class="test-result" v-if="testResult.navidrome">
            <el-alert
              :type="testResult.navidrome.ok ? 'success' : 'error'"
              :title="testResult.navidrome.ok ? '连接正常' : '连接失败'"
              :description="testResult.navidrome.detail || testResult.navidrome.error || ''"
              show-icon
              :closable="false"
            />
          </div>

          <el-button type="primary" plain :icon="Connection" :loading="testing === 'navidrome'" @click="test('navidrome')">
            测试连接
          </el-button>
        </el-card>
      </el-col>

      <el-col :xs="24" :lg="12">
        <el-card shadow="never">
          <template #header>
            <div class="card-head">
              <span class="card-title"><el-icon><FolderChecked /></el-icon> 飞牛音乐（trim.music）</span>
              <div class="toolbar">
                <el-tag v-if="!form.feiniu.enabled" size="small" type="warning" effect="plain">
                  未参与自动同步
                </el-tag>
                <el-tag size="small" :type="testResult.feiniu?.ok ? 'success' : testResult.feiniu ? 'danger' : 'info'">
                  {{ testLabel(testResult.feiniu) }}
                </el-tag>
              </div>
            </div>
          </template>

          <el-form label-width="96px" label-position="left">
            <el-form-item label="参与自动同步">
              <el-switch v-model="form.feiniu.enabled" />
              <span class="muted" style="margin-left: 10px; font-size: 12px">
                关闭后不再自动同步，仍可测试连接
              </span>
            </el-form-item>
            <el-form-item label="music.db 路径">
              <el-input v-model="form.feiniu.db_path" placeholder="/feiniu-db/music.db" />
            </el-form-item>
            <el-form-item label="结构校验">
              <el-button :icon="Document" :loading="schemaLoading" @click="checkSchema">校验表结构</el-button>
              <span class="muted" style="margin-left: 10px; font-size: 12px">
                确认 music.db 的表与列能匹配当前版本
              </span>
            </el-form-item>

            <el-form-item label="写入前备份">
              <el-switch v-model="form.feiniu.backup_db" />
              <span class="muted" style="margin-left: 10px; font-size: 12px">
                首次写入前自动备份，保留最近 5 份
              </span>
            </el-form-item>
          </el-form>

          <div class="test-result" v-if="testResult.feiniu">
            <el-alert
              :type="testResult.feiniu.ok ? 'success' : 'error'"
              :title="testResult.feiniu.ok ? '连接正常' : '连接失败'"
              :description="testResult.feiniu.detail || testResult.feiniu.error || ''"
              show-icon
              :closable="false"
            />
          </div>

          <el-button type="primary" plain :icon="Connection" :loading="testing === 'feiniu'" @click="test('feiniu')">
            测试连接
          </el-button>
          <el-button plain :icon="Picture" :loading="fixingCovers" @click="fixCovers">
            修复歌单封面
          </el-button>
        </el-card>
      </el-col>
    </el-row>

    <!-- db 模式结构校验结果 -->
    <el-card shadow="never" class="mt-12" v-if="schema">
      <template #header>
        <div class="card-head">
          <span class="card-title"><el-icon><Document /></el-icon> music.db 结构校验</span>
          <div class="toolbar">
            <el-tag size="small" :type="schema.ok ? 'success' : 'danger'">
              {{ schema.ok ? '结构匹配' : '结构不匹配' }}
            </el-tag>
            <el-tag v-if="schema.exists !== undefined" size="small" :type="schema.exists ? 'info' : 'danger'">
              数据库{{ schema.exists ? '存在' : '不存在' }}
            </el-tag>
            <el-tag v-if="schema.journal_mode" size="small" type="info" effect="plain">
              {{ schema.journal_mode }}
            </el-tag>
          </div>
        </div>
      </template>

      <el-descriptions :column="2" border size="small" class="mb-12">
        <el-descriptions-item label="db 路径">
          <span class="mono break-all">{{ schema.db_path || form.feiniu.db_path || '—' }}</span>
        </el-descriptions-item>
      </el-descriptions>

      <el-alert v-if="schema.error" :title="schema.error" type="error" show-icon :closable="false" class="mb-12" />

      <el-alert
        v-if="schema.missing && schema.missing.length"
        :title="`缺失 ${schema.missing.length} 项：${schema.missing.join('、')}`"
        type="warning"
        show-icon
        :closable="false"
        class="mb-12"
      />

      <el-collapse>
        <el-collapse-item
          v-for="(cols, table) in schema.tables || {}"
          :key="table"
          :name="table"
          :title="`${table}（${cols.length} 列）`"
        >
          <div class="cols">
            <el-tag v-for="c in cols" :key="c" size="small" effect="plain" class="mono">{{ c }}</el-tag>
          </div>
        </el-collapse-item>
      </el-collapse>
      <div v-if="!Object.keys(schema.tables || {}).length" class="empty-hint">未返回任何表结构</div>
    </el-card>
  </div>
</template>

<script setup>
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Check, Connection, Document, FolderChecked, Headset, Picture, Refresh } from '@element-plus/icons-vue'
import * as api from '../api.js'

const loading = ref(false)
const saving = ref(false)
const testing = ref('')
const schemaLoading = ref(false)

const schema = ref(null)
const testResult = reactive({ navidrome: null, feiniu: null })

const form = reactive({
  navidrome: { enabled: true, url: '', user: '', password: '' },
  feiniu: {
    enabled: true,
    db_path: '',
    backup_db: true,
  },
})

const testLabel = (r) => (r ? (r.ok ? '连接正常' : '连接失败') : '未测试')

const MASK = '***'

/** 后端返回的是掩码而不是真口令，统一抹成空串表示「未修改」。 */
const unmask = (v) => (v === MASK ? '' : v ?? '')

async function load() {
  loading.value = true
  try {
    const t = await api.getTargets()
    const n = t?.navidrome || {}
    form.navidrome.enabled = n.enabled ?? true
    form.navidrome.url = n.url ?? ''
    form.navidrome.user = n.user ?? ''
    // *** 是掩码，不能当成真密码回填（否则保存时会把 *** 原样写回去）
    form.navidrome.password = unmask(n.password)

    const f = t?.feiniu || {}
    form.feiniu.enabled = f.enabled ?? true
    form.feiniu.db_path = f.db_path ?? ''
    form.feiniu.backup_db = f.backup_db ?? true
  } catch (e) {
    api.toastError(e, '加载推送平台配置失败')
  } finally {
    loading.value = false
  }
}

/**
 * Navidrome 的 password 在顶层，后端把 "***" 和 "" 一样按「不修改」处理，
 * 所以可以无条件带上，不用判断用户到底输没输。
 */
function navidromeBody() {
  return {
    enabled: form.navidrome.enabled,
    url: form.navidrome.url,
    user: form.navidrome.user,
    password: form.navidrome.password,
  }
}

function feiniuBody() {
  return {
    enabled: form.feiniu.enabled,
    db_path: form.feiniu.db_path,
    backup_db: form.feiniu.backup_db,
  }
}

async function save() {
  saving.value = true
  try {
    await api.putTargets({ navidrome: navidromeBody(), feiniu: feiniuBody() })
    ElMessage.success('推送平台配置已保存')
    await load()
  } catch (e) {
    api.toastError(e, '保存失败')
  } finally {
    saving.value = false
  }
}

async function test(kind) {
  testing.value = kind
  try {
    // 带上界面上的当前值：用户多半是改完地址就来点测试，还没保存。
    // 不带的话测的是旧配置，报错信息跟屏幕上填的完全对不上。
    const draft = kind === 'feiniu' ? form.feiniu : form.navidrome
    const r = await api.testTarget(kind, draft)
    testResult[kind] = r
    if (r?.ok) ElMessage.success(kind === 'feiniu' ? '飞牛音乐连接正常' : 'Navidrome 连接正常')
    else ElMessage.warning(r?.error || '连接失败')
  } catch (e) {
    testResult[kind] = { ok: false, error: api.errText(e, '连接失败') }
    api.toastError(e, '连接测试失败')
  } finally {
    testing.value = ''
  }
}

async function checkSchema() {
  schemaLoading.value = true
  try {
    // 先把当前 db_path 写回，否则校验的是旧路径
    await api.putTargets({ feiniu: { db_path: form.feiniu.db_path } })
    schema.value = await api.getFeiniuSchema()
    if (schema.value?.ok) ElMessage.success('music.db 结构校验通过')
    else ElMessage.warning('music.db 结构不匹配，请查看缺失项')
  } catch (e) {
    api.toastError(e, '结构校验失败')
  } finally {
    schemaLoading.value = false
  }
}

const fixingCovers = ref(false)

async function fixCovers() {
  try {
    await ElMessageBox.confirm(
      '将把借自歌曲/专辑的封面实体复制到飞牛 playlist 封面目录，并修复缺失引用。继续？',
      '修复歌单封面',
      { type: 'warning', confirmButtonText: '开始修复', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  fixingCovers.value = true
  try {
    const r = await api.fixFeiniuCovers()
    let msg = `共 ${r.total ?? 0} 张：无需处理 ${r.ok ?? 0} · 已复制 ${r.copied ?? 0} · 已重选 ${r.reassigned ?? 0} · 缺封面 ${r.missing ?? 0}`
    if (r.cover_dir_exists === false) {
      msg += '\n警告：封面目录未挂载，请检查 compose 的 /feiniu-cover 挂载'
    }
    ElMessageBox.alert(msg, '封面修复结果', { confirmButtonText: '知道了' })
  } catch (e) {
    api.toastError(e, '封面修复失败')
  } finally {
    fixingCovers.value = false
  }
}

onMounted(load)
</script>

<style scoped>
.card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.card-title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-weight: 600;
  font-size: 14px;
}
.test-result {
  margin-bottom: 12px;
}
.cols {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
code {
  background: #f2f4f7;
  padding: 0 4px;
  border-radius: 3px;
}
</style>