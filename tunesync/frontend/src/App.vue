<template>
  <router-view v-if="isLogin" />

  <el-container v-else class="ts-layout">
    <el-aside v-if="!isNarrow" width="210px" class="ts-aside">
      <div class="ts-brand">
        <el-icon :size="20" color="#409eff"><Headset /></el-icon>
        <span class="ts-brand-text">TuneSync</span>
      </div>
      <div class="ts-brand-sub">歌单同步引擎</div>
      <el-menu
        :default-active="activePath"
        router
        class="ts-menu"
        :collapse="false"
        background-color="#20232a"
        text-color="#c9ccd4"
        active-text-color="#ffffff"
      >
        <el-menu-item v-for="m in menus" :key="m.path" :index="m.path">
          <el-icon><component :is="m.icon" /></el-icon>
          <span>{{ m.label }}</span>
        </el-menu-item>
      </el-menu>
    </el-aside>

    <!-- 窄屏：侧边收进抽屉，由顶栏的汉堡按钮唤出 -->
    <el-drawer v-model="drawer" direction="ltr" size="210px" :with-header="false" class="ts-drawer">
      <div class="ts-brand">
        <el-icon :size="20" color="#409eff"><Headset /></el-icon>
        <span class="ts-brand-text">TuneSync</span>
      </div>
      <div class="ts-brand-sub">歌单同步引擎</div>
      <el-menu
        :default-active="activePath"
        router
        class="ts-menu"
        :collapse="false"
        background-color="#ffffff"
        text-color="#303133"
        active-text-color="#409eff"
        @select="drawer = false"
      >
        <el-menu-item v-for="m in menus" :key="m.path" :index="m.path">
          <el-icon><component :is="m.icon" /></el-icon>
          <span>{{ m.label }}</span>
        </el-menu-item>
      </el-menu>
    </el-drawer>

    <el-container>
      <el-header height="52px" class="ts-header">
        <div class="ts-header-left">
          <el-button v-if="isNarrow" text :icon="Fold" class="hamburger" @click="drawer = true" />
          <div class="ts-header-title">{{ pageTitle }}</div>
        </div>
        <div class="toolbar">
          <el-tag v-if="anyRunning" type="primary" effect="dark" size="small" round>
            <el-icon style="vertical-align: -2px"><VideoPlay /></el-icon>
            运行中
          </el-tag>
          <el-tag v-if="state.queue.depth" type="warning" effect="plain" size="small" round>
            排队 {{ state.queue.depth }}
          </el-tag>
          <el-tooltip :content="wsTip" placement="bottom">
            <el-tag :type="wsTagType" size="small" effect="light" round class="ws-tag">
              <el-icon style="vertical-align: -2px"><component :is="wsIcon" /></el-icon>
              <!-- 窄屏只留状态图标，「进度通道断开，Ns 后重试」这类文案会把顶栏挤爆 -->
              <span v-if="!isNarrow">{{ wsText }}</span>
            </el-tag>
          </el-tooltip>
          <el-button v-if="ws.status !== 'open'" size="small" text :icon="Refresh" @click="reconnectNow">
            <span v-if="!isNarrow">重连</span>
          </el-button>
          <el-dropdown v-if="auth.enabled" trigger="click" @command="onUserCommand">
            <span class="user-chip">
              <el-icon style="vertical-align: -2px"><User /></el-icon>
              <span class="user-name">{{ auth.user?.username || '未登录' }}</span>
              <el-icon style="vertical-align: -2px"><ArrowDown /></el-icon>
            </span>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="password" :icon="Key">修改口令</el-dropdown-item>
                <el-dropdown-item command="logout" :icon="SwitchButton" divided>退出登录</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </el-header>

      <el-main class="ts-main">
        <router-view v-slot="{ Component }">
          <keep-alive :include="['Dashboard']">
            <component :is="Component" />
          </keep-alive>
        </router-view>
      </el-main>
    </el-container>

    <el-dialog v-model="pwdVisible" title="修改口令" width="420px">
      <el-form ref="pwdRef" :model="pwd" :rules="pwdRules" label-width="86px">
        <el-form-item label="原口令" prop="old_password">
          <el-input v-model="pwd.old_password" type="password" show-password />
        </el-form-item>
        <el-form-item label="新口令" prop="new_password">
          <el-input v-model="pwd.new_password" type="password" show-password placeholder="至少 6 位" />
        </el-form-item>
        <el-form-item label="确认新口令" prop="confirm">
          <el-input v-model="pwd.confirm" type="password" show-password @keyup.enter="submitPassword" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="pwdVisible = false">取消</el-button>
        <el-button type="primary" :loading="pwdBusy" @click="submitPassword">保存</el-button>
      </template>
    </el-dialog>
  </el-container>
</template>

<script setup>
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { ArrowDown, Fold, Headset, Key, Refresh, SwitchButton, User, VideoPlay } from '@element-plus/icons-vue'
import * as api from './api.js'
import { anyRunning, auth, markUnauthenticated, state, ws } from './store.js'
import { reconnectNow } from './ws.js'

const route = useRoute()
const router = useRouter()

/** 登录页自带整屏布局，不套侧边与顶栏。 */
const isLogin = computed(() => route.name === 'login')

// ───────────────────────────── 窄屏（手机端）

const drawer = ref(false)
const isNarrow = ref(false)
let mq = null

function applyNarrow() {
  isNarrow.value = !!mq?.matches
}

onMounted(() => {
  mq = window.matchMedia('(max-width: 768px)')
  applyNarrow()
  mq.addEventListener('change', applyNarrow)
})

onUnmounted(() => {
  mq?.removeEventListener('change', applyNarrow)
  mq = null
})

/** 从窄屏拉宽回桌面时，抽屉还开着会留一条遮罩。 */
watch(isNarrow, (narrow) => {
  if (!narrow) drawer.value = false
})

const menus = [
  { path: '/', label: '仪表盘', icon: 'Odometer' },
  { path: '/tasks', label: '推送计划', icon: 'Calendar' },
  { path: '/push-config', label: '新建计划', icon: 'Operation' },
  { path: '/sources', label: '音源管理', icon: 'Headset' },
  { path: '/playlists', label: '歌单广场', icon: 'Collection' },
  { path: '/playlist-manager', label: '歌单管理', icon: 'Delete' },
  { path: '/library', label: '本地乐库', icon: 'FolderOpened' },
  { path: '/targets', label: '推送平台', icon: 'Link' },
  { path: '/runs', label: '运行历史', icon: 'Clock' },
  { path: '/scrape', label: '刮削设置', icon: 'MagicStick' },
  { path: '/settings', label: '全局设置', icon: 'Setting' },
]

const activePath = computed(() => route.path)
const pageTitle = computed(() => route.meta?.title || 'TuneSync')

const wsTagType = computed(() => {
  if (ws.status === 'open') return 'success'
  if (ws.status === 'connecting') return 'warning'
  return 'info'
})

const wsIcon = computed(() => (ws.status === 'open' ? 'Link' : 'Warning'))

const wsText = computed(() => {
  if (ws.status === 'open') return '进度通道已连接'
  if (ws.status === 'connecting') return '正在连接进度通道'
  return `进度通道断开，${ws.retry}s 后重试`
})

const wsTip = computed(
  () => ws.status === 'open' ? ws.hello || '已连接 TuneSync 进度通道' : ws.lastError || '断线后会自动重连',
)

// ───────────────────────────── 账号

const pwdVisible = ref(false)
const pwdBusy = ref(false)
const pwdRef = ref(null)
const pwd = reactive({ old_password: '', new_password: '', confirm: '' })

const pwdRules = {
  old_password: [{ required: true, message: '请输入原口令', trigger: 'blur' }],
  new_password: [
    { required: true, message: '请输入新口令', trigger: 'blur' },
    { min: 6, message: '至少 6 位', trigger: 'blur' },
  ],
  confirm: [
    {
      validator: (_r, v, cb) => (v === pwd.new_password ? cb() : cb(new Error('两次输入不一致'))),
      trigger: 'blur',
    },
  ],
}

function onUserCommand(cmd) {
  if (cmd === 'password') {
    pwd.old_password = ''
    pwd.new_password = ''
    pwd.confirm = ''
    pwdVisible.value = true
  } else if (cmd === 'logout') {
    doLogout()
  }
}

async function doLogout() {
  try {
    await api.logout()
  } catch (e) {
    // 登出失败也要把人带离现场：本地标记已登出，后端会话下次自然失效
    api.toastError(e, '退出登录失败')
  }
  markUnauthenticated()
  router.replace({ name: 'login' })
}

async function submitPassword() {
  const ok = await pwdRef.value.validate().catch(() => false)
  if (!ok) return
  pwdBusy.value = true
  try {
    await api.changePassword({ old_password: pwd.old_password, new_password: pwd.new_password })
    ElMessage.success('口令已更新，请重新登录')
    pwdVisible.value = false
    markUnauthenticated()
    router.replace({ name: 'login' })
  } catch (e) {
    api.toastError(e, '修改口令失败')
  } finally {
    pwdBusy.value = false
  }
}
</script>

<style scoped>
.ts-layout {
  height: 100vh;
}
.ts-aside {
  background: #20232a;
  overflow: hidden;
}
.ts-brand {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 16px 16px 0;
  color: #fff;
}
.ts-brand-text {
  font-size: 17px;
  font-weight: 600;
  letter-spacing: 0.5px;
}
.ts-brand-sub {
  color: #7a8090;
  font-size: 11px;
  padding: 2px 16px 12px;
}
.ts-menu {
  border-right: none;
}
.ts-menu :deep(.el-menu-item) {
  height: 44px;
  line-height: 44px;
}
.ts-header {
  background: #fff;
  border-bottom: 1px solid #e6e8eb;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 20px;
}
.ts-header-left {
  display: flex;
  align-items: center;
  gap: 4px;
  min-width: 0;
}
.ts-header-title {
  font-size: 15px;
  font-weight: 600;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.hamburger {
  margin-left: -8px;
  flex: none;
}
.toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
  flex: none;
}
.ws-tag {
  cursor: default;
}
.user-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 4px 8px;
  border-radius: 15px;
  cursor: pointer;
  color: #606266;
  outline: none;
}
.user-chip:hover {
  background: #f2f4f7;
}
.user-name {
  max-width: 96px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ts-main {
  background: #f4f6f9;
  padding: 0;
  overflow-y: auto;
}

/* 抽屉走 teleport，scoped 选择器够不着，只能用 :global */
:global(.ts-drawer .el-drawer__body) {
  padding: 0;
  overflow-x: hidden;
}
:global(.ts-drawer .ts-brand) {
  color: #20232a;
}
:global(.ts-drawer .ts-brand-sub) {
  color: #909399;
}
:global(.ts-drawer .ts-menu) {
  border-right: none;
}
:global(.ts-drawer .ts-menu .el-menu-item) {
  height: 44px;
  line-height: 44px;
}

@media (max-width: 768px) {
  .ts-header {
    padding: 0 12px;
  }
}
</style>