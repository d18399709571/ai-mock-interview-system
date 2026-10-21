import { defineStore } from 'pinia'
import { ref, computed } from 'vue'

export const useAuthStore = defineStore('auth', () => {
  const token = ref(localStorage.getItem('admin_token') || '')
  const refreshToken = ref(localStorage.getItem('admin_refresh_token') || '')
  const email = ref(localStorage.getItem('admin_email') || '')

  const isLoggedIn = computed(() => !!token.value)

  function setAuth(data) {
    refreshToken.value = data.refresh_token
    localStorage.setItem('admin_refresh_token', data.refresh_token)
    token.value = data.access_token
    email.value = data.email || ''
    localStorage.setItem('admin_token', data.access_token)
    localStorage.setItem('admin_email', email.value)
  }

  function logout() {
    refreshToken.value = ''
    localStorage.removeItem('admin_refresh_token')
    token.value = ''
    email.value = ''
    localStorage.removeItem('admin_token')
    localStorage.removeItem('admin_email')
  }

  return { token, refreshToken, email, isLoggedIn, setAuth, logout }
})
