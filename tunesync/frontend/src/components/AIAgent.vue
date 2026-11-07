<template>
  <div class="ai-agent">
    <div class="agent-hero">
      <div class="hero-left">
        <div class="hero-icon">
          <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="1.8">
            <path d="M9 18V5l10-2v13" stroke-linecap="round" stroke-linejoin="round" />
            <circle cx="6" cy="18" r="3" /><circle cx="16" cy="16" r="3" />
          </svg>
        </div>
        <div class="hero-copy">
          <div class="hero-title">AI 音乐助手</div>
          <div class="hero-sub">说句话，帮你找歌单、做歌单，越用越懂你</div>
        </div>
      </div>
      <div class="hero-actions">
        <el-tag v-if="cfg?.configured" size="small" effect="dark" class="model-tag">{{ cfg.model }}</el-tag>
        <el-tag v-else size="small" type="warning" effect="plain">未配置模型</el-tag>
        <el-button size="small" round @click="openMemory">记忆 {{ memoryCount }}</el-button>
      </div>
    </div>

    <div class="mode-bar">
      <div class="mode-item">
        <span class="mode-label">推荐模式</span>
        <el-radio-group v-model="recommend" size="small">
          <el-radio-button value="user">我来选</el-radio-button>
          <el-radio-button value="smart">智能自动推送</el-radio-button>
        </el-radio-group>
        <span class="mode-hint">{{ recommend === 'smart' ? '自动创建并推送到飞牛 / Navidrome，无需确认' : '给你 5 张歌单，自己挑几张创建' }}</span>
      </div>
      <div class="mode-item">
        <span class="mode-label">生成模式</span>
        <el-radio-group v-model="generate" size="small">
          <el-radio-button value="fetch">拉取平台歌单</el-radio-button>
          <el-radio-button value="model">模型生成</el-radio-button>
        </el-radio-group>
        <span class="mode-hint">{{ generate === 'fetch' ? '智能体去音乐平台检索真实歌单' : '由模型给出歌单名与曲目清单' }}</span>
      </div>
    </div>

    <div ref="chatRef" class="chat-scroll">
      <div v-if="!messages.length" class="welcome">
        <div class="welcome-title">想听点什么？</div>
        <div class="welcome-sub">描述场景、心情、歌手，或者让我记住你的口味</div>
        <div class="chips">
          <el-button v-for="s in SUGGESTS" :key="s" size="small" round @click="quick(s)">{{ s }}</el-button>
        </div>
      </div>

      <div v-for="(m, mi) in messages" :key="mi" class="msg" :class="m.role">
        <div v-if="m.role === 'ai'" class="avatar">AI</div>
        <div class="bubble">
          <template v-if="m.role === 'user'">{{ m.text }}</template>
          <template v-else>
            <div v-if="m.thinking.length" class="think">
              <div class="think-head" @click="m.thinkOpen = !m.thinkOpen">
                <span class="think-title">思考过程</span>
                <span class="think-toggle">{{ m.thinkOpen ? '收起' : '展开' }}</span>
              </div>
              <div v-show="m.thinkOpen" class="think-body">
                <div v-for="(t, i) in m.thinking" :key="i" class="think-line">{{ t }}</div>
              </div>
            </div>

            <div class="stream-text">{{ displayStream(m.stream) }}<span v-if="!m.done && !m.stream" class="caret" /></div>
            <div v-if="m.error" class="note err">{{ m.error }}</div>
            <div v-if="m.creating" class="note">{{ m.creating }}</div>
            <div v-if="m.created" class="note ok">
              已加入创建队列 {{ m.created.created }} 张<span v-if="m.created.errors?.length">（{{ m.created.errors.length }} 张失败）</span>
            </div>
            <div v-if="m.memory" class="note memo">已记住：{{ m.memory }}</div>

            <div v-if="m.items.length" class="pl-box">
              <div class="pl-head">
                <span>为你找到 <b>{{ m.items.length }}</b> 张歌单</span>
                <el-button size="small" text type="primary" @click="m.expanded = !m.expanded">
                  {{ m.expanded ? '收起歌单列表' : '查看歌单列表' }}
                  <el-icon class="ml-4"><ArrowUp v-if="m.expanded" /><ArrowDown v-else /></el-icon>
                </el-button>
              </div>

              <div v-if="m.expanded" class="pl-grid">
                <div v-for="(it, ii) in m.items" :key="it.id" class="pl-card">
                  <div class="pl-cover">
                    <div class="cover-fallback">{{ (it.name || '歌').slice(0, 1) }}</div>
                    <img v-if="it.cover" :src="it.cover" referrerpolicy="no-referrer"
                         alt="" @error="(e) => { e.target.style.display = 'none' }" />
                    <span class="src-tag">{{ it.platform_name }}</span>
                  </div>
                  <div class="pl-info">
                    <div class="pl-name" :title="it.name">{{ it.name }}</div>
                    <div class="pl-meta">{{ it.count }} 首 · {{ it.creator || it.platform_name }}</div>
                    <div v-if="it.description" class="pl-desc" :title="it.description">{{ it.description }}</div>
                  </div>
                  <el-button size="small" type="primary" plain :loading="m.creatingIdx === ii"
                             :disabled="(m.createdSet || []).includes(ii)"
                             @click="createOne(m, ii)">
                    {{ (m.createdSet || []).includes(ii) ? '已加入队列' : '创建歌单' }}
                  </el-button>
                </div>
              </div>
              <el-button v-if="m.expanded && m.recommend !== 'smart' && m.items.length > 1"
                         size="small" class="mt-8" @click="createAll(m)">全部创建到平台</el-button>
            </div>
          </template>
        </div>
      </div>
    </div>

    <div class="composer">
      <el-input v-model="input" type="textarea" :rows="2" :maxlength="500" resize="none"
                placeholder="例如：来一份深夜开车听的国风电子，别老是那几首热门"
                @keydown.enter.exact.prevent="send" />
      <el-button class="send-btn" type="primary" :loading="busy" :disabled="!input.trim() || busy"
                 @click="send">发送</el-button>
    </div>

    <el-drawer v-model="memoryVisible" title="助手记忆" size="420px" class="mem-drawer">
      <div class="mem-section">
        <div class="mem-title">添加长期偏好</div>
        <div class="mem-add">
          <el-input v-model="newPref" size="small" placeholder="例如：我喜欢粤语老歌，别推抖音热榜"
                    @keydown.enter.prevent="addPref" />
          <el-button size="small" type="primary" :disabled="!newPref.trim()" @click="addPref">记住</el-button>
        </div>
        <div class="mem-hint">每次请求模型都会带上这些偏好，也会自动避开最近推荐过的歌单与歌曲。</div>
      </div>

      <div class="mem-section">
        <div class="mem-title">长期偏好（{{ prefs.length }}）</div>
        <div v-if="!prefs.length" class="muted">还没有显式偏好</div>
        <div v-for="p in prefs" :key="p.id" class="mem-row">
          <span>{{ p.content }}</span>
          <el-button size="small" text type="danger" @click="delPref(p.id)">删除</el-button>
        </div>
      </div>

      <div class="mem-section">
        <div class="mem-title">推荐历史（{{ history.length }}）</div>
        <div v-if="!history.length" class="muted">还没有推荐记录</div>
        <div v-for="h in history" :key="h.id" class="hist-row">
          <div class="hist-top">{{ h.content }}</div>
          <div class="hist-sub">{{ h.created_at }}</div>
          <div v-if="h.playlists?.length" class="hist-tags">
            <el-tag v-for="(n, i) in h.playlists.slice(0, 4)" :key="i" size="small" effect="plain">{{ n }}</el-tag>
          </div>
          <div v-if="h.tracks?.length" class="hist-songs">{{ h.tracks.slice(0, 10).join('、') }}</div>
        </div>
      </div>
    </el-drawer>
  </div>
</template>

<script setup>
import { computed, nextTick, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { ArrowDown, ArrowUp } from '@element-plus/icons-vue'
import * as api from '../api.js'

const SUGGESTS = [
  '深夜开车听的国风电子', '周深的冷门好歌', '小众民谣，别推热门', '记住我喜欢粤语老歌',
]

const input = ref('')
const busy = ref(false)
const recommend = ref('user')
const generate = ref('fetch')
const messages = ref([])
const chatRef = ref(null)
const cfg = ref(null)
const memoryVisible = ref(false)
const memData = ref({ memories: [], history: [] })
const newPref = ref('')

const prefs = computed(() => (memData.value.memories || []).filter((m) => m.kind === 'preference').reverse())
const history = computed(() => memData.value.history || [])
const memoryCount = computed(() => (memData.value.memories || []).length)

onMounted(async () => {
  try { cfg.value = await api.getAgentConfig() } catch { /* 不影响输入 */ }
  loadMemories()
})

async function loadMemories() {
  try { memData.value = await api.getAgentMemories() } catch { /* 静默 */ }
}
function openMemory() { memoryVisible.value = true; loadMemories() }
async function addPref() {
  const v = newPref.value.trim()
  if (!v) return
  try {
    await api.addAgentMemory(v)
    newPref.value = ''
    await loadMemories()
    ElMessage.success('已记住')
  } catch (e) { api.toastError(e, '添加失败') }
}
async function delPref(id) {
  try { await api.delAgentMemory(id); await loadMemories() } catch (e) { api.toastError(e, '删除失败') }
}

function displayStream(s) {
  return (s || '').split('---RESULT---')[0].trim()
}
function scroll() {
  nextTick(() => { if (chatRef.value) chatRef.value.scrollTop = chatRef.value.scrollHeight })
}
function quick(text) { input.value = text; send() }

async function send() {
  const text = input.value.trim()
  if (!text || busy.value) return
  messages.value.push({ role: 'user', text })
  const m = reactive({
    role: 'ai', text: '', stream: '', thinking: [], thinkOpen: true,
    items: [], expanded: false, memory: '', creating: '', created: null,
    error: '', done: false, recommend: recommend.value,
    createdSet: [], creatingIdx: -1,
  })
  messages.value.push(m)
  input.value = ''
  busy.value = true
  scroll()
  try {
    await api.agentChat({ message: text, recommend: recommend.value, generate: generate.value }, (ev) => {
      if (ev.type === 'thinking') m.thinking.push(ev.text)
      else if (ev.type === 'token') m.stream += ev.text
      else if (ev.type === 'playlists') {
        m.items = ev.items || []
        m.recommend = ev.recommend || m.recommend
        // 默认收起歌单列表，用户点「查看歌单列表」再展开
        m.expanded = false
        m.thinking = m.thinking.slice(0, 4)
      } else if (ev.type === 'creating') m.creating = ev.text
      else if (ev.type === 'created') { m.created = ev; m.creating = '' }
      else if (ev.type === 'memory') m.memory = ev.content
      else if (ev.type === 'error') m.error = ev.message
      else if (ev.type === 'done') m.done = true
      scroll()
    })
  } catch (e) {
    m.error = api.errText(e, '对话失败')
  } finally {
    m.done = true
    busy.value = false
    loadMemories()
    scroll()
  }
}

async function createOne(m, idx) {
  m.creatingIdx = idx
  try {
    const r = await api.createAgentPlaylists(m.items, [idx])
    if (!r.created) { ElMessage.warning(r.errors?.[0] || '创建失败'); return }
    m.createdSet.push(idx)
    ElMessage.success('已加入创建队列，可在「运行进度」查看')
  } catch (e) { api.toastError(e, '创建失败') } finally { m.creatingIdx = -1 }
}
async function createAll(m) {
  m.creatingIdx = -2
  try {
    const r = await api.createAgentPlaylists(m.items, m.items.map((_, i) => i))
    m.createdSet = m.items.map((_, i) => i)
    ElMessage.success(`已加入创建队列 ${r.created} 张`)
  } catch (e) { api.toastError(e, '创建失败') } finally { m.creatingIdx = -1 }
}
</script>

<style scoped>
.ai-agent {
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 16px;
  overflow: hidden;
  background: var(--el-bg-color);
  box-shadow: 0 6px 24px rgba(80, 70, 160, 0.08);
  margin-bottom: 12px;
}
.agent-hero {
  display: flex; align-items: center; justify-content: space-between; gap: 12px;
  padding: 16px 18px;
  background: linear-gradient(120deg, #5b4bdb 0%, #7d5bd6 48%, #b25fb0 100%);
  color: #fff;
}
.hero-left, .hero-actions { display: flex; align-items: center; gap: 10px; }
.hero-actions { flex-wrap: wrap; justify-content: flex-end; }
.hero-icon {
  width: 40px; height: 40px; border-radius: 12px; flex: 0 0 40px;
  display: flex; align-items: center; justify-content: center;
  background: rgba(255, 255, 255, 0.18);
}
.hero-title { font-size: 16px; font-weight: 600; letter-spacing: 0.3px; }
.hero-sub { font-size: 12px; opacity: 0.86; margin-top: 2px; }
.model-tag { background: rgba(255, 255, 255, 0.22) !important; border: none !important; color: #fff !important; }

.mode-bar {
  display: flex; flex-wrap: wrap; gap: 8px 24px;
  padding: 12px 18px; border-bottom: 1px solid var(--el-border-color-lighter);
  background: var(--el-fill-color-blank);
}
.mode-item { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.mode-label { font-size: 12px; font-weight: 600; color: var(--el-text-color-regular); }
.mode-hint { font-size: 12px; color: var(--el-text-color-secondary); }

.chat-scroll { height: 380px; overflow-y: auto; padding: 16px 18px; }
.welcome { text-align: center; padding: 42px 12px; }
.welcome-title { font-size: 18px; font-weight: 600; }
.welcome-sub { font-size: 13px; color: var(--el-text-color-secondary); margin-top: 6px; }
.chips { display: flex; flex-wrap: wrap; gap: 8px; justify-content: center; margin-top: 18px; }

.msg { display: flex; gap: 8px; margin-bottom: 14px; align-items: flex-start; }
.msg.user { justify-content: flex-end; }
.avatar {
  width: 28px; height: 28px; border-radius: 9px; flex: 0 0 28px;
  display: flex; align-items: center; justify-content: center;
  font-size: 11px; font-weight: 600; color: #fff;
  background: linear-gradient(135deg, #6a5cff, #b06ab3);
}
.bubble { max-width: min(760px, 88%); border-radius: 12px; padding: 10px 14px; font-size: 13px; line-height: 1.7; }
.msg.user .bubble { background: var(--el-color-primary); color: #fff; border-top-right-radius: 3px; }
.msg.ai .bubble { background: var(--el-fill-color-light); border-top-left-radius: 3px; }

.think { margin-bottom: 8px; border: 1px dashed var(--el-border-color); border-radius: 8px; padding: 6px 10px; }
.think-head { display: flex; justify-content: space-between; cursor: pointer; font-size: 12px; color: var(--el-text-color-secondary); }
.think-title { font-weight: 600; }
.think-toggle { color: var(--el-color-primary); }
.think-body { margin-top: 6px; font-size: 12px; color: var(--el-text-color-regular); }
.think-line { padding: 2px 0; }
.stream-text { white-space: pre-wrap; word-break: break-word; }
.caret { display: inline-block; width: 6px; height: 14px; background: var(--el-color-primary); vertical-align: -2px; animation: blink 1s step-start infinite; }
@keyframes blink { 50% { opacity: 0; } }
.note { margin-top: 8px; font-size: 12px; padding: 6px 10px; border-radius: 7px; background: var(--el-fill-color); }
.note.err { color: var(--el-color-danger); }
.note.ok { color: var(--el-color-success); }
.note.memo { color: var(--el-color-primary); background: var(--el-color-primary-light-9); }

.pl-box { margin-top: 12px; border-top: 1px solid var(--el-border-color-lighter); padding-top: 10px; }
.pl-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; font-size: 13px; }
.pl-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 10px; margin-top: 10px; }
.pl-card {
  display: flex; flex-direction: column; gap: 8px;
  border: 1px solid var(--el-border-color-lighter); border-radius: 10px; padding: 10px;
  background: var(--el-bg-color);
}
.pl-cover { position: relative; width: 100%; aspect-ratio: 1 / 0.62; border-radius: 8px; overflow: hidden; }
.pl-cover img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; }
.cover-fallback {
  position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
  font-size: 30px; font-weight: 600; color: rgba(255, 255, 255, 0.9);
  background: linear-gradient(135deg, #6a5cff, #b06ab3);
}
.src-tag {
  position: absolute; left: 6px; top: 6px; z-index: 2;
  font-size: 11px; padding: 2px 7px; border-radius: 20px; color: #fff;
  background: rgba(0, 0, 0, 0.55); backdrop-filter: blur(4px);
}
.pl-info { min-width: 0; }
.pl-name { font-size: 13px; font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.pl-meta, .pl-desc { font-size: 11px; color: var(--el-text-color-secondary); margin-top: 2px; }
.pl-desc { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ml-4 { margin-left: 4px; }
.mt-8 { margin-top: 10px; }

.composer { display: flex; gap: 10px; align-items: flex-end; padding: 12px 18px 16px; border-top: 1px solid var(--el-border-color-lighter); }
.send-btn { height: 52px; padding: 0 20px; border-radius: 10px; }

.mem-section { margin-bottom: 20px; }
.mem-title { font-size: 13px; font-weight: 600; margin-bottom: 10px; }
.mem-add { display: flex; gap: 8px; }
.mem-hint, .muted { font-size: 12px; color: var(--el-text-color-secondary); line-height: 1.6; }
.mem-hint { margin-top: 8px; }
.mem-row, .hist-row { border: 1px solid var(--el-border-color-lighter); border-radius: 8px; padding: 8px 10px; margin-bottom: 8px; font-size: 12px; }
.mem-row { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.hist-top { font-size: 12px; line-height: 1.6; }
.hist-sub { font-size: 11px; color: var(--el-text-color-tertiary); margin-top: 2px; }
.hist-tags { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 6px; }
.hist-songs { font-size: 11px; color: var(--el-text-color-secondary); margin-top: 6px; line-height: 1.6; }

@media (max-width: 768px) {
  .agent-hero { padding: 12px 14px; flex-wrap: wrap; }
  .hero-actions { justify-content: flex-start; }
  .mode-bar { padding: 10px 14px; gap: 8px; }
  .mode-item { width: 100%; }
  .mode-hint { width: 100%; }
  .chat-scroll { height: 52vh; padding: 12px 14px; }
  .bubble { max-width: 92%; }
  .pl-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .composer { padding: 10px 14px 12px; }
  .send-btn { height: 48px; padding: 0 14px; }
  .welcome { padding: 28px 6px; }
}
@media (max-width: 420px) {
  .pl-grid { grid-template-columns: minmax(0, 1fr); }
  .hero-sub { font-size: 11px; }
}
</style>

<style>
@media (max-width: 768px) { .mem-drawer { width: 100% !important; } }
</style>
