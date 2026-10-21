<template>
  <div class="auth-page">
    <div class="auth-card">
      <h2>🎯 注册智面</h2>
      <p class="subtitle">创建账号开始 AI 模拟面试</p>
      <form v-if="step === 'register'" @submit.prevent="handleRegister">
        <div class="form-row">
          <div class="form-group">
            <label>名</label>
            <input v-model="form.first_name" placeholder="名" required />
          </div>
          <div class="form-group">
            <label>姓</label>
            <input v-model="form.last_name" placeholder="姓" required />
          </div>
        </div>
        <div class="form-group">
          <label>邮箱</label>
          <input v-model="form.email" type="email" placeholder="请输入邮箱" required />
        </div>
        <div class="form-group">
          <label>密码</label>
          <input v-model="form.password" type="password" placeholder="至少8位，含字母+数字+特殊字符" required />
        </div>
        <button type="submit" class="btn-glow" :disabled="loading">
          {{ loading ? '注册中...' : '注册' }}
        </button>
      </form>
      <form v-else @submit.prevent="handleVerify">
        <p class="subtitle">邮箱尚未验证，请输入邮件中的六位验证码。</p>
        <div class="form-group">
          <label for="verification-email">待验证邮箱</label>
          <input id="verification-email" v-model.trim="verificationEmail" @change="savePending" type="email" required />
        </div>
        <div class="form-group">
          <label for="verification-code">邮箱验证码</label>
          <input id="verification-code" v-model="code" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{6}" maxlength="6" placeholder="六位数字验证码" required />
        </div>
        <button type="submit" class="btn-glow" :disabled="loading || resending">
          {{ loading ? '验证中...' : '验证并登录' }}
        </button>
        <button type="button" class="step-link" @click="handleResend" :disabled="loading || resending || remaining > 0 || !verificationEmail">
          {{ resending ? '发送中...' : remaining > 0 ? `${remaining} 秒后可重发` : '重发验证码' }}
        </button>
        <p class="subtitle">邮箱填错可返回注册；也可输入已注册的邮箱继续验证。</p>
        <button type="button" class="step-link" @click="backToRegistration" :disabled="loading || resending">返回注册</button>
      </form>
      <p v-if="error" class="error" role="alert">{{ error }}</p>
      <p v-if="success" class="success" role="status">{{ success }}</p>
      <button v-if="step === 'register'" type="button" class="step-link" @click="openVerification()">已注册？继续验证邮箱</button>
      <p class="auth-link">已有账号？<router-link to="/login">去登录</router-link></p>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted, onUnmounted } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import { register, verifyEmail, sendRegistrationCode } from '../api/auth'

const router = useRouter()
const authStore = useAuthStore()
const form = reactive({ first_name: '', last_name: '', email: '', password: '' })
const error = ref('')
const success = ref('')
const loading = ref(false)
const step = ref('register')
const verificationEmail = ref('')
const code = ref('')
const resending = ref(false)
const resendAt = ref(0)
const now = ref(Date.now())
const remaining = computed(() => Math.max(0, Math.ceil((resendAt.value - now.value) / 1000)))
const pendingKey = 'pendingRegistration'
let timer

function savePending() {
  // Only an email and cooldown timestamp are resumable; never persist credentials.
  try { sessionStorage.setItem(pendingKey, JSON.stringify({ email: verificationEmail.value, resendAt: resendAt.value })) } catch {}
}

function cooldown(seconds) {
  now.value = Date.now()
  resendAt.value = now.value + seconds * 1000
  savePending()
}

function openVerification(email = form.email.trim()) {
  verificationEmail.value = email
  code.value = ''
  step.value = 'verify'
  error.value = ''; success.value = ''
  savePending()
}

function backToRegistration() {
  step.value = 'register'; code.value = ''; form.password = ''
  error.value = ''; success.value = ''
  try { sessionStorage.removeItem(pendingKey) } catch {}
}

function requestError(e) {
  return e.message === 'Network Error' ? '网络连接失败，请检查网络后重试' : e.message || '请求失败，请稍后重试'
}

onMounted(() => {
  try {
    const pending = JSON.parse(sessionStorage.getItem(pendingKey))
    if (typeof pending?.email === 'string' && pending.email) {
      verificationEmail.value = pending.email
      resendAt.value = Number.isFinite(pending.resendAt) ? pending.resendAt : 0
      step.value = 'verify'
    }
  } catch {}
  timer = setInterval(() => { now.value = Date.now() }, 1000)
})
onUnmounted(() => clearInterval(timer))

async function handleRegister() {
  if (loading.value) return
  error.value = ''; success.value = ''; loading.value = true
  try {
    const res = await register(form)
    if (!res?.verification_required || !res.email) throw new Error('注册响应无效，请检查邮箱后继续验证')
    form.password = ''
    openVerification(res.email)
    cooldown(60) // The registration endpoint starts the server's 60-second send limit.
    success.value = '注册成功，请先验证邮箱'
  } catch (e) { error.value = requestError(e) }
  finally { loading.value = false }
}

async function handleVerify() {
  if (loading.value || resending.value) return
  error.value = ''; success.value = ''
  if (!/^\d{6}$/.test(code.value)) { error.value = '请输入六位数字验证码'; return }
  loading.value = true
  savePending()
  try {
    const data = await verifyEmail(verificationEmail.value, code.value)
    if (typeof data?.access_token !== 'string' || !data.access_token.trim()
      || typeof data?.refresh_token !== 'string' || !data.refresh_token.trim() || data.user?.is_verified !== true) {
      throw new Error('验证响应未包含有效登录凭证，请前往登录页重试')
    }
    authStore.setAuth(data)
    code.value = ''
    try { sessionStorage.removeItem(pendingKey) } catch {}
    router.push('/dashboard')
  } catch (e) { error.value = requestError(e) }
  finally { loading.value = false }
}

async function handleResend() {
  if (loading.value || resending.value || remaining.value > 0 || !verificationEmail.value) return
  error.value = ''; success.value = ''; resending.value = true
  try {
    const data = await sendRegistrationCode(verificationEmail.value)
    cooldown(Number.isFinite(data?.can_resend_at) && data.can_resend_at > 0 ? data.can_resend_at : 60)
    success.value = '验证码已发送，请检查邮箱'
  } catch (e) {
    error.value = requestError(e)
    if (e.message?.includes('每分钟只能发送一次')) cooldown(60)
  } finally { resending.value = false }
}
</script>

<style scoped>
.auth-page { display: flex; align-items: center; justify-content: center; min-height: 100vh; background: linear-gradient(135deg, #f5f7fa 0%, #e4e8f0 100%); }
.auth-card { width: 420px; background: white; border-radius: 16px; padding: 36px; text-align: center; box-shadow: 0 4px 24px rgba(0,0,0,0.08); }
.auth-card h2 { font-size: 24px; color: #4f46e5; margin-bottom: 4px; }
.subtitle { color: #6b7280; margin-bottom: 24px; font-size: 14px; }
.form-row { display: flex; gap: 12px; }
.form-group { margin-bottom: 16px; text-align: left; flex: 1; }
.form-group label { display: block; margin-bottom: 6px; font-size: 14px; font-weight: 500; color: #374151; }
.form-group input { width: 100%; padding: 11px 14px; border: 1px solid #e5e7eb; border-radius: 10px; font-size: 14px; outline: none; transition: all 0.3s; background: #f9fafb; }
.form-group input:focus { border-color: #4f46e5; box-shadow: 0 0 0 3px rgba(79,70,229,0.1); background: white; }
.error { color: #ef4444; font-size: 13px; margin-bottom: 12px; }
.success { color: #10b981; font-size: 13px; margin-bottom: 12px; }
.btn-glow { width: 100%; padding: 12px; border: none; border-radius: 10px; font-size: 15px; font-weight: 600; color: white; background: linear-gradient(135deg, #4f46e5, #7c3aed, #6366f1); background-size: 200% 200%; animation: gradientShift 3s ease infinite; cursor: pointer; transition: all 0.3s; box-shadow: 0 4px 15px rgba(79,70,229,0.4); }
.btn-glow:hover { transform: translateY(-1px); box-shadow: 0 6px 25px rgba(79,70,229,0.5); }
.btn-glow:disabled { opacity: 0.6; cursor: not-allowed; transform: none; }
@keyframes gradientShift { 0% { background-position: 0% 50%; } 50% { background-position: 100% 50%; } 100% { background-position: 0% 50%; } }
.auth-link { margin-top: 18px; font-size: 14px; color: #6b7280; }
.auth-link a { color: #4f46e5; font-weight: 500; }
.step-link { margin: 12px 0; border: 0; background: none; color: #4f46e5; cursor: pointer; }
.step-link:disabled { color: #9ca3af; cursor: default; }
</style>
