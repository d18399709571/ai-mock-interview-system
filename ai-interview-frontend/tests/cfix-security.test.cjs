// Synthetic component checks. Real browser/HTTP evidence lives outside the repository.
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const { test } = require('node:test')
const root = path.resolve(__dirname, '..')
const read = file => fs.readFileSync(path.join(root, 'src', file), 'utf8')
const ref = value => ({ value })

async function load(code, deps, globals = {}) {
  const context = vm.createContext({ console, ...globals })
  const module = new vm.SourceTextModule(code, { context })
  await module.link(async name => {
    assert.ok(Object.hasOwn(deps, name), `Unexpected import ${name}`)
    const values = deps[name]
    const child = new vm.SyntheticModule(Object.keys(values), function () {
      for (const [key, value] of Object.entries(values)) this.setExport(key, value)
    }, { context })
    await child.link(() => { throw Error('Nested import forbidden') })
    await child.evaluate()
    return child
  })
  await module.evaluate()
  return module.namespace
}

async function registration(api = {}, saved = null) {
  const values = new Map(saved ? [['pendingRegistration', JSON.stringify(saved)]] : [])
  const calls = [], mounts = []
  const script = read('views/Register.vue').match(/<script setup>([\s\S]*?)<\/script>/)[1]
  const view = await load(script + '\nexport { form, step, code, verificationEmail, remaining, loading, resending, error, success, handleRegister, handleVerify, handleResend, openVerification, backToRegistration };', {
    vue: { ref, reactive: x => x, computed: fn => ({ get value() { return fn() } }), onMounted: fn => mounts.push(fn), onUnmounted() {} },
    'vue-router': { useRouter: () => ({ push: url => calls.push(['navigate', url]) }) },
    '../stores/auth': { useAuthStore: () => ({ setAuth: data => calls.push(['auth', data]) }) },
    '../api/auth': {
      register: async () => ({ verification_required: true, email: 'synthetic@example.com' }),
      verifyEmail: async () => ({ access_token: 'synthetic-access', refresh_token: 'synthetic-refresh', user: { is_verified: true } }),
      sendRegistrationCode: async () => ({ can_resend_at: 60 }), ...api
    }
  }, { sessionStorage: { getItem: k => values.get(k), setItem: (k, v) => values.set(k, v), removeItem: k => values.delete(k) }, setInterval: () => 1, clearInterval() {} })
  mounts.forEach(fn => fn())
  return { view, calls, values }
}

test('Registration enters verification without authenticating or persisting credentials', async () => {
  const { view, calls, values } = await registration()
  view.form.password = 'synthetic password'
  await view.handleRegister()
  assert.equal(view.step.value, 'verify'); assert.equal(view.form.password, '')
  assert.deepEqual(calls, []); assert.ok(view.remaining.value > 0)
  assert.deepEqual(Object.keys(JSON.parse(values.get('pendingRegistration'))).sort(), ['email', 'resendAt'])
})

test('Pending email resumes after refresh; manual recovery and returning to registration remain available', async () => {
  const { view, values } = await registration({}, { email: 'synthetic@example.com', resendAt: Date.now() + 30000 })
  assert.equal(view.step.value, 'verify'); assert.equal(view.code.value, '')
  assert.ok(view.remaining.value > 0)
  view.backToRegistration(); assert.equal(view.step.value, 'register'); assert.equal(values.size, 0)
  view.openVerification('other@example.com'); assert.equal(view.verificationEmail.value, 'other@example.com')
})

test('Only a verified response with both credentials establishes a session', async () => {
  const good = await registration()
  good.view.code.value = '000000'; await good.view.handleVerify()
  assert.deepEqual(good.calls.map(x => x[0]), ['auth', 'navigate']); assert.equal(good.values.size, 0)
  for (const data of [{ verification_required: true }, { access_token: 'a', refresh_token: '', user: { is_verified: true } }, { access_token: 'a', refresh_token: 'b', user: { is_verified: false } }]) {
    const fixture = await registration({ verifyEmail: async () => data })
    fixture.view.code.value = '000000'; await fixture.view.handleVerify()
    assert.equal(fixture.calls.length, 0); assert.match(fixture.view.error.value, /有效登录凭证/)
  }
})

test('Malformed code never calls HTTP; rejected or disconnected verification never logs in', async () => {
  let requests = 0
  const malformed = await registration({ verifyEmail: async () => { requests++ } })
  malformed.view.code.value = '123'; await malformed.view.handleVerify(); assert.equal(requests, 0)
  for (const message of ['验证码无效或已过期', 'Network Error', '账号已停用']) {
    const fixture = await registration({ verifyEmail: async () => { throw Error(message) } })
    fixture.view.code.value = '000000'; await fixture.view.handleVerify()
    assert.equal(fixture.calls.length, 0); assert.equal(fixture.view.loading.value, false)
    assert.ok(fixture.view.error.value); assert.equal(fixture.values.size, 1)
    assert.ok(!fixture.values.get('pendingRegistration').includes('000000'))
  }
})

test('Resend honors cooldown and actual response; server limit and network failures remain visible', async () => {
  let requests = 0
  const fixture = await registration({ sendRegistrationCode: async () => { requests++; return { can_resend_at: 35 } } })
  fixture.view.openVerification('synthetic@example.com'); await fixture.view.handleResend(); await fixture.view.handleResend()
  assert.equal(requests, 1); assert.ok(fixture.view.remaining.value <= 35 && fixture.view.remaining.value > 0)
  for (const message of ['每分钟只能发送一次验证码', 'Network Error']) {
    const failing = await registration({ sendRegistrationCode: async () => { throw Error(message) } })
    failing.view.openVerification('synthetic@example.com'); await failing.view.handleResend()
    assert.ok(failing.view.error.value); assert.equal(failing.view.success.value, '')
    assert.equal(failing.view.resending.value, false)
    assert.equal(failing.view.remaining.value > 0, message.includes('每分钟'))
  }
})

test('Email API functions reuse the original client and exact endpoint contracts', async () => {
  const calls = []
  const api = await load(read('api/auth.js'), { './request': { default: { post: (...x) => calls.push(x) } } })
  api.verifyEmail('synthetic@example.com', '000000'); api.sendRegistrationCode('synthetic@example.com')
  assert.deepEqual(JSON.parse(JSON.stringify(calls)), [
    ['/auth/verify-email', { email: 'synthetic@example.com', code: '000000' }],
    ['/auth/send-verification-code', { email: 'synthetic@example.com', code_type: 'registration' }]
  ])
})

async function interview(getInterviews) {
  const script = read('views/Interview.vue').match(/<script setup>([\s\S]*?)<\/script>/)[1]
  let submits = 0
  const view = await load(script + '\nexport { totalQuestions, finished, metadataError, loadInterviewMetadata, answer, handleSubmit };', {
    vue: { ref, computed: fn => fn, onMounted() {}, nextTick: fn => fn?.() },
    'vue-router': { useRoute: () => ({ params: { id: '7' } }) },
    '../stores/auth': { useAuthStore: () => ({}) },
    '../api/interview': { getInterviews, getMessages: async () => [], getReport: async () => ({}), submitAnswerStream: async () => { submits++ } }
  })
  return { view, submits: () => submits }
}

test('Three/five-question metadata and completed state come from the server', async () => {
  for (const total of [3, 5]) {
    const { view } = await interview(async () => ({ items: [{ interview_id: 7, total_questions: total, status: 'completed' }] }))
    assert.equal(view.totalQuestions.value, null); await view.loadInterviewMetadata()
    assert.equal(view.totalQuestions.value, total); assert.equal(view.finished.value, true)
  }
})

test('Missing/invalid/failed metadata stays unknown and blocks submission; retry recovers', async () => {
  for (const response of [{ items: [] }, { items: [{ interview_id: 7, total_questions: 0 }] }, { items: [{ interview_id: 7, total_questions: '5' }] }, null]) {
    let retry = false
    const fixture = await interview(async () => {
      if (retry) return { items: [{ interview_id: 7, total_questions: 3, status: 'in_progress' }] }
      if (response === null) throw Error('synthetic unavailable')
      return response
    })
    await fixture.view.loadInterviewMetadata(); fixture.view.answer.value = 'Synthetic answer'; await fixture.view.handleSubmit()
    assert.equal(fixture.submits(), 0); assert.equal(fixture.view.totalQuestions.value, null); assert.ok(fixture.view.metadataError.value)
    retry = true; await fixture.view.loadInterviewMetadata()
    assert.equal(fixture.view.totalQuestions.value, 3); assert.equal(fixture.view.metadataError.value, '')
  }
})
