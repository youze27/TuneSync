<template>
  <div class="login-wrap">
    <el-card class="login-card" shadow="always">
      <div class="login-brand">
        <el-icon :size="30" color="#409eff"><Headset /></el-icon>
        <span>TuneSync</span>
      </div>

      <template v-if="auth.ssoReady && !auth.authenticated">
        <el-alert type="warning" :closable="false" show-icon
                  title="等待统一身份认证" description="请通过已配置的统一身份认证入口（SSO）访问本页面。" />
      </template>

      <el-form v-else ref="formRef" :model="form" :rules="rules" label-position="top" @submit.prevent>
        <div class="login-title">{{ needSetup ? '初始化管理员账号' : '登录 TuneSync' }}</div>
        <div class="login-sub">
          {{ needSetup
            ? '这是首次启用登录校验，请创建管理员账号。'
            : '请输入账号与口令。' }}
        </div>

        <el-form-item label="用户名" prop="username">
          <el-input v-model="form.username" placeholder="用户名" autocomplete="username" clearable />
        </el-form-item>

        <el-form-item label="口令" prop="password">
          <el-input
            v-model="form.password"
            type="password"
            placeholder="口令"
            autocomplete="current-password"
            show-password
            @keyup.enter="submit"
          />
        </el-form-item>

        <el-form-item v-if="needSetup" label="确认口令" prop="confirm">
          <el-input
            v-model="form.confirm"
            type="password"
            placeholder="再输一次"
            autocomplete="new-password"
            show-password
            @keyup.enter="submit"
          />
        </el-form-item>

        <el-button type="primary" class="login-submit" :loading="busy" @click="submit">
          {{ needSetup ? '创建并进入' : '登录' }}
        </el-button>
      </el-form>
    </el-card>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Headset } from '@element-plus/icons-vue'
import * as api from '../api.js'
import { auth, loadAuth } from '../store.js'

const route = useRoute()
const router = useRouter()

const formRef = ref(null)
const busy = ref(false)
const form = reactive({ username: '', password: '', confirm: '' })

const needSetup = computed(() => auth.needSetup)

// needSetup 要等鉴权状态回来才确定，所以规则跟着它一起算，别在 setup 时就定死。
const rules = computed(() => ({
  username: [{ required: true, message: '请输入用户名', trigger: 'blur' }],
  password: [
    { required: true, message: '请输入口令', trigger: 'blur' },
    ...(needSetup.value ? [{ min: 6, message: '口令至少 6 位', trigger: 'blur' }] : []),
  ],
  confirm: [
    {
      validator: (_r, value, cb) =>
        value === form.password ? cb() : cb(new Error('两次输入的口令不一致')),
      trigger: 'blur',
    },
  ],
}))

onMounted(async () => {
  // 从别的页面跳过来时用户名可以预填，省一次输入
  if (route.query.username) form.username = String(route.query.username)
  if (!auth.checked) await loadAuth().catch(() => {})
})

async function submit() {
  const ok = await formRef.value.validate().catch(() => false)
  if (!ok) return
  busy.value = true
  try {
    if (needSetup.value) await api.setupAccount({ username: form.username, password: form.password })
    else await api.login({ username: form.username, password: form.password })
    await loadAuth(true)
    ElMessage.success(needSetup.value ? '账号已创建' : '登录成功')
    router.replace(String(route.query.redirect || '/'))
  } catch (e) {
    ElMessage.error(api.errText(e, needSetup.value ? '创建账号失败' : '登录失败'))
  } finally {
    busy.value = false
  }
}
</script>

<style scoped>
.login-wrap {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
  background: linear-gradient(160deg, #20232a 0%, #2f3742 60%, #3c4654 100%);
}
.login-card {
  width: 100%;
  max-width: 380px;
  border-radius: 12px;
}
.login-brand {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 10px;
  font-size: 22px;
  font-weight: 600;
  margin-bottom: 18px;
}
.login-title {
  font-size: 16px;
  font-weight: 600;
}
.login-sub {
  color: #909399;
  font-size: 12px;
  margin: 4px 0 14px;
}
.login-submit {
  width: 100%;
}
</style>
