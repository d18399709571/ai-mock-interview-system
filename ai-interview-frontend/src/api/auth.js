import api from './request'

export function register(data) {
  return api.post('/auth/register', data)
}

export function verifyEmail(email, code) {
  return api.post('/auth/verify-email', { email, code })
}

export function sendRegistrationCode(email) {
  return api.post('/auth/send-verification-code', { email, code_type: 'registration' })
}

export function login(email, password) {
  return api.post('/auth/login', { email, password })
}

export function getMe() {
  return api.get('/auth/me')
}

export function logout(refreshToken) {
  return api.post('/auth/logout', { refresh_token: refreshToken })
}
