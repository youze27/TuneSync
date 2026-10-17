import { createRouter, createWebHistory } from 'vue-router'
import { auth, loadAuth } from './store.js'

const routes = [
  { path: '/login', name: 'login', component: () => import('./pages/Login.vue'), meta: { title: '登录' } },
  { path: '/', name: 'dashboard', component: () => import('./pages/Dashboard.vue'), meta: { title: '仪表盘' } },
  { path: '/tasks', name: 'tasks', component: () => import('./pages/Tasks.vue'), meta: { title: '推送计划' } },
  { path: '/push-config', name: 'push-config', component: () => import('./pages/PushConfig.vue'), meta: { title: '新建计划' } },
  { path: '/sources', name: 'sources', component: () => import('./pages/Sources.vue'), meta: { title: '音源管理' } },
  { path: '/playlists', name: 'playlists', component: () => import('./pages/Playlists.vue'), meta: { title: '歌单广场' } },
  { path: '/playlist-manager', name: 'playlist-manager', component: () => import('./pages/PlaylistManager.vue'), meta: { title: '歌单管理' } },
  { path: '/library', name: 'library', component: () => import('./pages/Library.vue'), meta: { title: '本地乐库' } },
  { path: '/targets', name: 'targets', component: () => import('./pages/Targets.vue'), meta: { title: '推送平台' } },
  { path: '/scrape', name: 'scrape', component: () => import('./pages/Scrape.vue'), meta: { title: '刮削设置' } },
  { path: '/runs', name: 'runs', component: () => import('./pages/Runs.vue'), meta: { title: '运行历史' } },
  { path: '/settings', name: 'settings', component: () => import('./pages/Settings.vue'), meta: { title: '全局设置' } },
  // 旧书签 / 旧链接：推送偏好已合并进推送计划，指向新页面
  { path: '/preferences', redirect: '/push-config' },
  { path: '/:pathMatch(.*)*', redirect: '/' },
]

const router = createRouter({
  history: createWebHistory('/'),
  routes,
})

// 未登录时把业务页统统弹回登录页。auth.enabled=false 时这里完全不拦。
router.beforeEach(async (to) => {
  if (to.name === 'login') return true
  if (!auth.checked) {
    try {
      await loadAuth()
    } catch (e) {
      // 状态接口本身失败：宁可放行到业务页让它各自报错，也不要把人锁在门外
      return true
    }
  }
  if (auth.enabled && !auth.authenticated) {
    return { name: 'login', query: { redirect: to.fullPath } }
  }
  return true
})

router.afterEach((to) => {
  document.title = to.meta?.title ? `${to.meta.title} · TuneSync` : 'TuneSync 管理界面'
})

export default router
