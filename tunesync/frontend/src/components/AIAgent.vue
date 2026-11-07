<template>
  <div class="ai-agent">
    <!-- 页头：只保留标题 + 模式图标 + 记忆 + 个性化 -->
    <div class="agent-bar">
      <div class="bar-left">
        <span class="bar-dot" />
        <span class="bar-title">AI 音乐助手</span>
        <el-popover placement="bottom-start" trigger="click" :width="250" popper-class="mode-popper">
          <template #reference>
            <el-button text circle :icon="Setting" class="mode-icon" title="模式设置" />
          </template>
          <div class="mode-pop">
            <div class="pop-title">推荐模式</div>
            <el-radio-group v-model="recommend" size="small" class="pop-group">
              <el-radio-button value="user">由我挑选</el-radio-button>
              <el-radio-button value="smart">智能直推</el-radio-button>
            </el-radio-group>
            <div class="pop-hint">{{ recommend === 'smart' ? '自动创建并推送到飞牛 / Navidrome' : '给出 5 张歌单，自己挑几张创建' }}</div>
            <div class="pop-title">生成模式</div>
            <el-radio-group v-model="generate" size="small" class="pop-group">
              <el-radio-button value="fetch">平台拉取</el-radio-button>
              <el-radio-button value="model">模型创作</el-radio-button>
            </el-radio-group>
            <div class="pop-hint">{{ generate === 'fetch' ? '去音乐平台检索真实歌单（带封面）' : '模型给出歌单名与曲目清单（无封面）' }}</div>
            <div class="pop-foot">选项会自动记住，下次打开保持</div>
          </div>
        </el-popover>
      </div>

      <div class="bar-actions">
        <el-button size="small" text @click="openMemory">记忆<span v-if="memoryCount"> {{ memoryCount }}</span></el-button>
        <el-popover trigger="click" placement="bottom-end" :width="330" popper-class="persona-popper" @show="onPersona(true)">
          <template #reference>
            <el-button size="small" text class="persona-btn">
              个性化 <el-icon class="ml-2"><ArrowDown /></el-icon>
            </el-button>
          </template>
          <div class="persona">
                <div class="persona-head">
                  <b>个性化</b>
                  <span class="persona-src">{{ (taste.sources || []).join(' · ') || '暂无数据源' }}</span>
                </div>
                <div class="persona-switch">
                  <el-switch v-model="useTaste" size="small" />
                  <span>参考我的真实听歌记录</span>
                </div>
                <template v-if="taste.available">
                  <div class="p-line">
                    最近 {{ taste.stats?.plays || 0 }} 次播放 · {{ taste.stats?.tracks || 0 }} 首 ·
                    {{ taste.stats?.artists || 0 }} 位歌手
                  </div>
                  <div v-if="taste.top_artists?.length" class="p-tags">
                    <el-tag v-for="a in taste.top_artists.slice(0, 5)" :key="a.name" size="small" effect="plain">
                      {{ a.name }}
                    </el-tag>
                  </div>
                  <div class="p-section">习惯与近期变化</div>
                  <div v-for="(s, i) in taste.insights || []" :key="i" class="p-insight">· {{ s }}</div>
                  <div v-if="taste.library_playlists?.length" class="p-section">
                    曲库已有歌单（避免生成雷同主题）
                  </div>
                  <div v-if="taste.library_playlists?.length" class="p-playlists">
                    {{ taste.library_playlists.slice(0, 6).map((x) => x.name).join('、') }}
                  </div>
                </template>
                <div v-else class="muted">{{ taste.reason || '正在读取听歌记录…' }}</div>
                <div class="persona-foot">
                  <el-button size="small" :loading="tasteLoading" @click="refreshTaste">刷新画像</el-button>
                  <el-button size="small" type="primary" plain :disabled="!taste.available" @click="pinTaste">
                    存为长期偏好
                  </el-button>
                </div>
                <div class="persona-tip">画像来自 Spindle / Navidrome / ListenBrainz，只读，不改动你的曲库</div>
          </div>
        </el-popover>
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

      <div v-for="(m, mi) in messages" :key="m.id || mi" class="msg" :class="m.role">
        <div v-if="m.role === 'ai'" class="avatar">AI</div>
        <div class="bubble">
          <template v-if="m.role === 'user'">{{ m.text }}</template>
          <template v-else>
            <div v-if="m.phase && !m.done" class="phase"><span class="phase-dot" />{{ m.phase }}</div>
            <div v-if="m.thinking.length" class="think">
              <div class="think-head" @click="m.thinkOpen = !m.thinkOpen">
                <span class="think-title">思考过程（{{ m.thinking.length }}）</span>
                <span class="think-toggle">{{ m.thinkOpen ? '收起' : '展开' }}</span>
              </div>
              <div v-show="m.thinkOpen" class="think-body">
                <div v-for="(t, i) in m.thinking" :key="i" class="think-line">{{ t }}</div>
              </div>
            </div>

            <div v-if="displayStream(m.stream)" class="stream-text">
              {{ displayStream(m.stream) }}<span v-if="!m.done" class="caret" />
            </div>
            <div v-else-if="!m.done" class="typing"><i /><i /><i /></div>
            <div v-if="m.error" class="note err">{{ m.error }}</div>
            <div v-if="m.creating" class="note">{{ m.creating }}</div>
            <div v-if="m.created" class="note ok">
              已加入创建队列 {{ m.created.created }} 张<span v-if="m.created.errors?.length">（{{ m.created.errors.length }} 张失败）</span>
            </div>
            <div v-if="m.memory" class="note memo">已记住：{{ m.memory }}</div>

            <div v-if="m.items.length" class="pl-box">
              <div class="pl-head">
                <span>为你找到 <b>{{ m.items.length }}</b> 张歌单<span class="pl-sub"> · 点击「歌曲列表」可展开曲目</span></span>
                <el-button v-if="m.recommend !== 'smart' && m.items.length > 1" size="small" text type="primary"
                           :loading="m.creatingIdx === -2" @click="createAll(m)">全部创建</el-button>
              </div>

              <div class="pl-list">
                <div v-for="(it, ii) in m.items" :key="it.id + '-' + ii" class="pl-card">
                  <div class="pl-row">
                    <div v-if="showCover(m, it)" class="pl-cover">
                      <div class="cover-fallback">{{ (it.name || '歌').slice(0, 1) }}</div>
                      <img v-if="it.cover" :src="it.cover" referrerpolicy="no-referrer" alt=""
                           @error="(e) => { e.target.style.display = 'none' }" />
                      <span class="src-tag">{{ it.platform_name }}</span>
                    </div>
                    <div class="pl-info">
                      <div class="pl-name" :title="it.name">
                        {{ it.name }}<span v-if="!showCover(m, it)" class="src-inline">{{ it.platform_name }}</span>
                      </div>
                      <div class="pl-meta">{{ it.count }} 首 · {{ it.creator || it.platform_name }}</div>
                      <div v-if="it.description" class="pl-desc" :title="it.description">{{ it.description }}</div>
                    </div>
                    <el-button class="pl-btn" size="small" type="primary" plain
                               :loading="m.creatingIdx === ii"
                               :disabled="(m.createdSet || []).includes(ii)"
                               @click="createOne(m, ii)">
                      {{ (m.createdSet || []).includes(ii) ? '已加入' : '创建' }}
                    </el-button>
                  </div>
                  <div class="tracks-toggle" @click="toggleTracks(m, it)">
                    歌曲列表<span v-if="it.tracks?.length"> ({{ it.tracks.length }})</span>
                    <el-icon><ArrowUp v-if="it.tracksOpen" /><ArrowDown v-else /></el-icon>
                  </div>
                  <div v-if="it.tracksOpen" class="tracks-box">
                    <div v-if="it.tracksLoading" class="muted">正在读取曲目…</div>
                    <div v-else-if="!it.tracks?.length" class="muted">暂无曲目信息</div>
                    <ol v-else class="track-list">
                      <li v-for="(t, ti) in it.tracks.slice(0, 60)" :key="ti">
                        <span class="tk-title">{{ t.title }}</span><span class="tk-artist">{{ t.artist }}</span>
                      </li>
                    </ol>
                    <div v-if="it.tracks?.length > 60" class="muted">仅显示前 60 首</div>
                  </div>
                </div>
              </div>
            </div>
          </template>
        </div>
      </div>
    </div>

    <div class="composer">
      <div class="composer-main">
        <el-input v-model="input" type="textarea" :rows="2" :maxlength="500" resize="none"
                  placeholder="例如：来一份深夜开车听的国风电子，别老是那几首热门"
                  @keydown.enter.exact.prevent="send" />
        <div class="composer-hint">{{ modeBrief }}</div>
      </div>
      <el-button class="send-btn" type="primary" :loading="busy" :disabled="!input.trim() || busy"
                 @click="send">发送</el-button>
    </div>

    <el-drawer v-model="memoryVisible" title="助手记忆" size="420px" class="mem-drawer">
      <div class="mem-section mem-clear">
        <span class="muted">对话历史会保留在对话框里；清空只删对话，不动长期偏好。</span>
        <el-button size="small" text type="danger" @click="clearConversation">清空对话</el-button>
      </div>
      <div class="mem-section">
        <div class="mem-title">添加长期偏好</div>
        <div class="mem-add">
          <el-input v-model="newPref" size="small" placeholder="例如：我喜欢粤语老歌，别推抖音热榜"
                    @keydown.enter.prevent="addPref" />
          <el-button size="small" type="primary" :disabled="!newPref.trim()" @click="addPref">记住</el-button>
        </div>
        <div class="mem-hint">每次请求模型都会带上偏好、真实听歌画像与最近推荐过的歌单，自动避开雷同。</div>
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
        <div class="mem-title">口味变化记忆</div>
        <div v-if="!tasteMems.length" class="muted">还没有记录到变化</div>
        <div v-for="p in tasteMems" :key="p.id" class="mem-row"><span>{{ p.content }}</span></div>
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
import { computed, nextTick, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { ArrowDown, ArrowUp, Setting } from '@element-plus/icons-vue'
import * as api from '../api.js'

const SUGGESTS = [
  '深夜开车听的国风电子', '周深的冷门好歌', '小众民谣，别推热门', '记住我喜欢粤语老歌',
]
const MODES_KEY = 'ts_agent_modes_v1'
const TASTE_KEY = 'ts_agent_use_taste'

function loadModes() {
  try { return JSON.parse(localStorage.getItem(MODES_KEY) || '{}') || {} } catch { return {} }
}
const saved = loadModes()
const recommend = ref(saved.recommend === 'smart' ? 'smart' : 'user')
const generate = ref(saved.generate === 'model' ? 'model' : 'fetch')
const useTaste = ref(localStorage.getItem(TASTE_KEY) !== '0')
watch([recommend, generate], () => {
  localStorage.setItem(MODES_KEY, JSON.stringify({ recommend: recommend.value, generate: generate.value }))
})
watch(useTaste, (v) => localStorage.setItem(TASTE_KEY, v ? '1' : '0'))

const MODE_LABEL = {
  user: '由我挑选', smart: '智能直推', fetch: '平台拉取', model: '模型创作',
}
const modeBrief = computed(() => `当前：${MODE_LABEL[recommend.value]} · ${MODE_LABEL[generate.value]}`)

const input = ref('')
const busy = ref(false)
const messages = ref([])
const chatRef = ref(null)
const cfg = ref(null)
const memoryVisible = ref(false)
const memData = ref({ memories: [], history: [] })
const newPref = ref('')
const taste = ref({ available: false, sources: [], insights: [] })
const tasteLoading = ref(false)
let msgSeq = 0

const prefs = computed(() => (memData.value.memories || []).filter((m) => m.kind === 'preference').reverse())
const tasteMems = computed(() => (memData.value.memories || []).filter((m) => m.kind === 'taste').reverse())
const history = computed(() => memData.value.history || [])
const memoryCount = computed(() => (memData.value.memories || []).length)

onMounted(async () => {
  try { cfg.value = await api.getAgentConfig() } catch { /* 不影响输入 */ }
  loadMemories()
  loadTaste()
  loadConversation()
})

async function loadTaste(force = false) {
  tasteLoading.value = true
  try { taste.value = await api.getAgentTaste(force) } catch { /* 静默降级 */ }
  finally { tasteLoading.value = false }
}
const refreshTaste = () => loadTaste(true)
const onPersona = (visible) => { if (visible) loadTaste() }
async function pinTaste() {
  const p = taste.value
  if (!p.available) return
  const text = ('我的听歌画像：' + (p.insights || []).slice(0, 3).join('；')).slice(0, 480)
  try {
    await api.addAgentMemory(text)
    await loadMemories()
    ElMessage.success('已写入长期偏好')
  } catch (e) { api.toastError(e, '保存失败') }
}

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

function newMessage(role, extra = {}) {
  return reactive({
    id: ++msgSeq, role, text: '', stream: '', thinking: [], thinkOpen: false,
    phase: '', items: [], memory: '', creating: '', created: null, error: '',
    done: true, recommend: 'user', generate: 'fetch', createdSet: [], creatingIdx: -1,
    ...extra,
  })
}

async function loadConversation() {
  try {
    const r = await api.getAgentHistory(40)
    const list = r.messages || []
    messages.value = list.map((h) => {
      if (h.role === 'user') return newMessage('user', { text: h.content })
      const items = (h.items || []).map((it) => ({ ...it, tracksOpen: false, tracksLoading: false }))
      return newMessage('ai', {
        stream: h.content || '', items, done: true,
        recommend: h.recommend || 'user', generate: h.generate || 'fetch',
      })
    })
  } catch { /* 历史拿不到就当新会话 */ }
  scroll()
}
async function clearConversation() {
  try { await api.clearAgentHistory(); messages.value = []; ElMessage.success('对话已清空') }
  catch (e) { api.toastError(e, '清空失败') }
}

function displayStream(s) {
  return (s || '').split('---RESULT---')[0].trim()
}
function scroll() {
  nextTick(() => { if (chatRef.value) chatRef.value.scrollTop = chatRef.value.scrollHeight })
}
function quick(text) { input.value = text; send() }
function showCover(m, it) {
  return m.generate !== 'model' && !it.no_cover
}

async function toggleTracks(m, it) {
  it.tracksOpen = !it.tracksOpen
  if (!it.tracksOpen || it.tracks?.length || it.tracksLoading) return
  if (it.kind === 'generated' || !it.platform || !it.id) return
  it.tracksLoading = true
  try {
    const r = await api.getAgentTracks(it.platform, it.id)
    it.tracks = r.tracks || []
  } catch (e) {
    it.tracks = []
    ElMessage.warning(api.errText(e, '曲目读取失败'))
  } finally { it.tracksLoading = false }
}

async function send() {
  const text = input.value.trim()
  if (!text || busy.value) return
  messages.value.push(newMessage('user', { text }))
  const m = newMessage('ai', { done: false, recommend: recommend.value, generate: generate.value })
  messages.value.push(m)
  input.value = ''
  busy.value = true
  scroll()

  // 即使上游代理把 SSE 帧攒成一大块，也按固定速率把字符吐进对话框，
  // 保证用户看到的是「逐字输出」而不是等结束后一次性蹦出来。
  let pending = []
  let timer = null
  const tick = () => {
    if (!pending.length) { timer = null; return }
    m.stream += pending.splice(0, 3).join('')
    scroll()
    timer = setTimeout(tick, 12)
  }
  const push = (t) => { for (const ch of t) pending.push(ch); if (!timer) tick() }
  const flush = () => {
    if (timer) { clearTimeout(timer); timer = null }
    if (pending.length) { m.stream += pending.join(''); pending = []; scroll() }
  }

  try {
    await api.agentChat({ message: text, recommend: recommend.value,
                          generate: generate.value, use_taste: useTaste.value }, (ev) => {
      if (ev.type === 'heartbeat') {
        if (!m.stream && !m.items.length && !m.error) m.phase = `模型思考中… ${ev.elapsed}s`
      } else if (ev.type === 'thinking') { m.thinking.push(ev.text); m.phase = ev.text }
      else if (ev.type === 'token') { if (!m.stream) m.phase = '模型正在输出…'; push(ev.text) }
      else if (ev.type === 'playlists') {
        flush()
        m.items = (ev.items || []).map((it) => ({ ...it, tracksOpen: false, tracksLoading: false }))
        m.recommend = ev.recommend || m.recommend
        m.generate = ev.generate || m.generate
        m.phase = ''
      } else if (ev.type === 'creating') { flush(); m.creating = ev.text; m.phase = ev.text }
      else if (ev.type === 'created') { m.created = ev; m.creating = '' }
      else if (ev.type === 'memory') m.memory = ev.content
      else if (ev.type === 'error') { flush(); m.error = ev.message; m.phase = '' }
      else if (ev.type === 'done') { flush(); m.done = true; m.phase = '' }
      scroll()
    })
  } catch (e) {
    flush()
    m.error = api.errText(e, '对话失败')
  } finally {
    flush()
    m.done = true
    m.phase = ''
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
  border-radius: 14px;
  overflow: hidden;
  background: var(--el-bg-color);
  box-shadow: 0 6px 24px rgba(80, 70, 160, 0.08);
  margin-bottom: 12px;
}

/* 页头：一行，约 42px，把高度全部让给聊天窗口 */
.agent-bar {
  display: flex; align-items: center; justify-content: space-between; gap: 8px;
  padding: 6px 12px 6px 14px; min-height: 42px;
  border-bottom: 1px solid var(--el-border-color-lighter);
  background: linear-gradient(100deg, #f7f5ff 0%, #fdf6ff 55%, #fff 100%);
}
.bar-left, .bar-actions { display: flex; align-items: center; gap: 6px; min-width: 0; }
.bar-dot {
  width: 7px; height: 7px; border-radius: 50%; flex: none;
  background: #7d5bd6; box-shadow: 0 0 0 3px rgba(125, 91, 214, 0.14);
}
.bar-title { font-size: 14px; font-weight: 600; letter-spacing: 0.3px; white-space: nowrap; }
.mode-icon { font-size: 15px; padding: 4px; }
.persona-btn { --el-button-text-color: var(--el-text-color-regular); }
.ml-2 { margin-left: 2px; }

.chat-scroll { height: 380px; overflow-y: auto; padding: 14px 16px; }
.welcome { text-align: center; padding: 34px 12px; }
.welcome-title { font-size: 17px; font-weight: 600; }
.welcome-sub { font-size: 12px; color: var(--el-text-color-secondary); margin-top: 6px; }
.chips { display: flex; flex-wrap: wrap; gap: 8px; justify-content: center; margin-top: 16px; }

.msg { display: flex; gap: 8px; margin-bottom: 12px; align-items: flex-start; }
.msg.user { justify-content: flex-end; }
.avatar {
  width: 26px; height: 26px; border-radius: 8px; flex: 0 0 26px;
  display: flex; align-items: center; justify-content: center;
  font-size: 10px; font-weight: 600; color: #fff;
  background: linear-gradient(135deg, #6a5cff, #b06ab3);
}
.bubble { max-width: min(860px, 92%); border-radius: 11px; padding: 9px 12px; font-size: 13px; line-height: 1.65; }
.msg.user .bubble { background: var(--el-color-primary); color: #fff; border-top-right-radius: 3px; }
.msg.ai .bubble { background: var(--el-fill-color-light); border-top-left-radius: 3px; min-width: 0; }
.phase { display: flex; align-items: center; gap: 6px; font-size: 12px; color: var(--el-text-color-secondary); margin-bottom: 6px; }
.phase-dot { width: 6px; height: 6px; border-radius: 50%; background: #7d5bd6; animation: pulse 1.2s ease-in-out infinite; }
@keyframes pulse { 50% { opacity: 0.25; transform: scale(0.7); } }
.think { margin-bottom: 6px; border: 1px dashed var(--el-border-color); border-radius: 7px; padding: 4px 8px; }
.think-head { display: flex; justify-content: space-between; cursor: pointer; font-size: 11px; color: var(--el-text-color-secondary); }
.think-title { font-weight: 600; }
.think-toggle { color: var(--el-color-primary); }
.think-body { margin-top: 4px; font-size: 11px; color: var(--el-text-color-regular); }
.think-line { padding: 1px 0; }
.stream-text { white-space: pre-wrap; word-break: break-word; }
.caret { display: inline-block; width: 5px; height: 13px; margin-left: 2px; background: #7d5bd6; vertical-align: -2px; animation: blink 1s step-start infinite; }
@keyframes blink { 50% { opacity: 0; } }
.typing { display: flex; gap: 4px; padding: 4px 0; }
.typing i { width: 5px; height: 5px; border-radius: 50%; background: #9a8ad8; animation: pulse 1s infinite; }
.typing i:nth-child(2) { animation-delay: 0.15s; }
.typing i:nth-child(3) { animation-delay: 0.3s; }
.note { margin-top: 6px; font-size: 11px; padding: 5px 8px; border-radius: 6px; background: var(--el-fill-color); }
.note.err { color: var(--el-color-danger); }
.note.ok { color: var(--el-color-success); }
.note.memo { color: var(--el-color-primary); background: var(--el-color-primary-light-9); }

/* 歌单：5 张全部展开，横向紧凑卡片，封面在左 */
.pl-box { margin-top: 10px; border-top: 1px solid var(--el-border-color-lighter); padding-top: 8px; }
.pl-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; font-size: 12px; margin-bottom: 8px; }
.pl-sub { color: var(--el-text-color-secondary); }
.pl-list { display: flex; flex-direction: column; gap: 6px; }
.pl-card { border: 1px solid var(--el-border-color-lighter); border-radius: 9px; background: var(--el-bg-color); overflow: hidden; }
.pl-row { display: flex; align-items: center; gap: 9px; padding: 7px 9px; }
.pl-cover {
  position: relative; width: 46px; height: 46px; flex: 0 0 46px;
  border-radius: 7px; overflow: hidden; background: linear-gradient(135deg, #6a5cff, #b06ab3);
}
.pl-cover img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; }
.cover-fallback { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; font-size: 19px; font-weight: 600; color: #fff; }
.src-tag {
  position: absolute; left: 2px; bottom: 2px; z-index: 2; font-size: 9px;
  padding: 0 4px; border-radius: 8px; color: #fff; background: rgba(0, 0, 0, 0.55);
}
.pl-info { flex: 1; min-width: 0; }
.pl-name { font-size: 12.5px; font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.src-inline { margin-left: 6px; font-size: 10px; font-weight: 400; color: #7d5bd6; }
.pl-meta, .pl-desc { font-size: 10.5px; color: var(--el-text-color-secondary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.pl-desc { margin-top: 1px; }
.pl-btn { flex: none; padding: 0 9px; height: 26px; font-size: 11px; }
.tracks-toggle {
  display: flex; align-items: center; gap: 3px; cursor: pointer; user-select: none;
  padding: 4px 9px; font-size: 11px; color: var(--el-text-color-secondary);
  border-top: 1px dashed var(--el-border-color-lighter);
}
.tracks-toggle:hover { color: var(--el-color-primary); background: var(--el-fill-color-light); }
.tracks-box { padding: 6px 10px 8px; border-top: 1px solid var(--el-border-color-lighter); background: var(--el-fill-color-blank); }
.track-list { margin: 0; padding-left: 20px; max-height: 210px; overflow-y: auto; font-size: 11px; line-height: 1.7; }
.track-list li { display: flex; justify-content: space-between; gap: 8px; }
.tk-title { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.tk-artist { color: var(--el-text-color-secondary); flex: none; max-width: 45%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

.composer { display: flex; gap: 8px; align-items: flex-end; padding: 9px 12px 11px; border-top: 1px solid var(--el-border-color-lighter); }
.composer-main { flex: 1; min-width: 0; }
.composer-hint { font-size: 10.5px; color: var(--el-text-color-secondary); margin-top: 4px; }
.send-btn { height: 46px; padding: 0 18px; border-radius: 9px; flex: none; }

.mem-section { margin-bottom: 18px; }
.mem-clear { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.mem-title { font-size: 13px; font-weight: 600; margin-bottom: 8px; }
.mem-add { display: flex; gap: 8px; }
.mem-hint, .muted { font-size: 11.5px; color: var(--el-text-color-secondary); line-height: 1.6; }
.mem-hint { margin-top: 6px; }
.mem-row, .hist-row { border: 1px solid var(--el-border-color-lighter); border-radius: 8px; padding: 7px 9px; margin-bottom: 7px; font-size: 12px; }
.mem-row { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.hist-top { font-size: 12px; line-height: 1.6; }
.hist-sub { font-size: 11px; color: var(--el-text-color-tertiary); margin-top: 2px; }
.hist-tags { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 5px; }
.hist-songs { font-size: 11px; color: var(--el-text-color-secondary); margin-top: 5px; line-height: 1.6; }

@media (max-width: 768px) {
  .agent-bar { padding: 5px 8px 5px 10px; }
  .bar-title { font-size: 13px; }
  .chat-scroll { padding: 10px 8px; }
  .avatar { display: none; }              /* 手机端不显示 AI 头像，宽度全给输出框 */
  .msg { gap: 0; }
  .bubble { max-width: 100%; width: 100%; padding: 8px 10px; }
  .msg.user .bubble { max-width: 88%; width: auto; }
  .pl-row { gap: 7px; padding: 6px 7px; }
  .pl-cover { width: 42px; height: 42px; flex-basis: 42px; }
  .composer { padding: 8px 8px 10px; gap: 6px; }
  .send-btn { height: 44px; padding: 0 12px; }
  .welcome { padding: 24px 4px; }
}
</style>

<style>
.mode-popper { padding: 4px 6px !important; }
.mode-pop { font-size: 12px; }
.mode-pop .pop-title { font-weight: 600; margin: 4px 2px 6px; }
.mode-pop .pop-group { width: 100%; margin-bottom: 4px; }
.mode-pop .pop-group .el-radio-button { flex: 1; }
.mode-pop .pop-group .el-radio-button__inner { width: 100%; }
.mode-pop .pop-hint { color: #909399; font-size: 11px; line-height: 1.5; margin: 0 2px 8px; }
.mode-pop .pop-foot { border-top: 1px solid #ebeef5; padding-top: 6px; color: #a8abb2; font-size: 10.5px; }
.persona-popper { padding: 0 !important; max-width: calc(100vw - 24px); }
.persona { padding: 12px 14px; max-height: 66vh; overflow-y: auto; }
.persona-head { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; margin-bottom: 10px; }
.persona-src { font-size: 10px; color: #909399; }
.persona-switch { display: flex; align-items: center; gap: 8px; font-size: 12px; margin-bottom: 10px; }
.p-line { font-size: 12px; margin-bottom: 6px; }
.p-tags { display: flex; flex-wrap: wrap; gap: 4px; margin-bottom: 8px; }
.p-section { font-size: 11px; font-weight: 600; color: #606266; margin: 10px 0 4px; }
.p-insight { font-size: 11px; color: #606266; line-height: 1.6; }
.p-playlists { font-size: 11px; color: #909399; line-height: 1.6; }
.persona-foot { display: flex; gap: 8px; margin-top: 12px; }
.persona-tip { font-size: 10px; color: #a8abb2; margin-top: 8px; line-height: 1.5; }
@media (max-width: 768px) { .mem-drawer { width: 100% !important; } }
</style>
