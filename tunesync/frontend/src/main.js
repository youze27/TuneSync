import { createApp } from 'vue'
import ElementPlus from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import * as ElementPlusIconsVue from '@element-plus/icons-vue'

import 'element-plus/dist/index.css'
import './styles.css'

import App from './App.vue'
import router from './router.js'
import { bindWs, boot, markUnauthenticated } from './store.js'
import { connect } from './ws.js'

const app = createApp(App)
for (const [key, comp] of Object.entries(ElementPlusIconsVue)) {
  app.component(key, comp)
}
app.use(ElementPlus, { locale: zhCn })
app.use(router)
app.mount('#app')

// 会话中途过期时（cookie 到期、手动清 cookie、后端重启清了会话表），
// api.js 抛的 401 会走到这里：记住已登出并把人送回登录页。
window.addEventListener('ts:unauthorized', () => {
  markUnauthenticated()
  if (router.currentRoute.value.name !== 'login') {
    router.replace({ name: 'login', query: { redirect: router.currentRoute.value.fullPath } })
  }
})

// 先拉鉴权与字典（分类/平台）再连 WS，避免首屏页面拿不到分组数据。
boot().finally(() => {
  bindWs()
  connect()
})