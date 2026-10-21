// Node/Vue checks with synthetic transport; no browser or real HTTP requests.
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const { test } = require('node:test')

const root = path.resolve(__dirname, '../..')
const source = file => fs.readFileSync(path.join(root, file), 'utf8')
const ref = value => ({ value })
const store = () => ({ token: 'synthetic-access', refreshToken: 'synthetic-refresh',
  logout() { this.token = ''; this.refreshToken = '' } })

async function moduleFor(code, globals = {}, dependencies = {}) {
  const context = vm.createContext({ TextDecoder, ...globals })
  const cache = new Map()
  async function dependency(name) {
    if (!cache.has(name)) {
      assert.ok(Object.hasOwn(dependencies, name), `Unexpected import: ${name}`)
      const exports = dependencies[name]
      const child = new vm.SyntheticModule(Object.keys(exports), function () {
        for (const [key, value] of Object.entries(exports)) this.setExport(key, value)
      }, { context })
      await child.link(() => { throw new Error('Nested import forbidden') })
      await child.evaluate()
      cache.set(name, child)
    }
    return cache.get(name)
  }
  const module = new vm.SourceTextModule(code, { context,
    importModuleDynamically: dependency })
  await module.link(dependency)
  await module.evaluate()
  return module.namespace
}

async function apiFor(response) {
  const auth = store()
  const chunks = []
  const reader = {
    async read() {
      if (chunks.length) {
        const next = chunks.shift()
        if (next instanceof Error) throw next
        return { done: false, value: Buffer.from(next) }
      }
      return { done: true }
    },
    async cancel() {}, releaseLock() {}
  }
  const output = { ok: true, status: 200,
    headers: { get: () => 'text/event-stream' }, body: { getReader: () => reader },
    ...response }
  let requests = 0
  const bodies = []
  const api = await moduleFor(source('ai-interview-frontend/src/api/interview.js'),
    { fetch: async (url, options) => { requests++; bodies.push(JSON.parse(options.body)); return output } },
    { './request': { default: { post: async (url, body) => bodies.push(body) } }, '../stores/auth': { useAuthStore: () => auth } })
  return { api, chunks, bodies, requests: () => requests }
}

test('SSE handles split chunks and exactly one success', async () => {
  const { api, chunks, requests } = await apiFor()
  chunks.push('data: {"type":"chunk","content":"hello"}\n\n',
    'data: {"type":"do', 'ne","score":7}\n\n')
  const received = []; let done = 0
  await api.submitAnswerStream(1, 'synthetic', 0, x => received.push(x), () => done++)
  assert.deepEqual(received, ['hello']); assert.equal(done, 1); assert.equal(requests(), 1)
})

test('SSE rejects HTTP 401/500 before reading a stream', async () => {
  for (const status of [401, 500]) {
    const { api } = await apiFor({ ok: false, status, body: null })
    await assert.rejects(api.submitAnswerStream(1, 'synthetic', 0, () => {}, () => {}), /HTTP/)
  }
})

test('SSE rejects absent body or incorrect content type', async () => {
  for (const response of [{ body: null }, { headers: { get: () => 'application/json' } }]) {
    const { api } = await apiFor(response)
    await assert.rejects(api.submitAnswerStream(1, 'synthetic', 0, () => {}, () => {}))
  }
})

test('SSE rejects EOF without done and malformed events', async () => {
  for (const text of ['', 'data: {"type":"chunk","content":"hello"}\n\n', 'data: {bad}\n\n']) {
    const { api, chunks, requests } = await apiFor()
    if (text) chunks.push(text)
    let done = 0
    await assert.rejects(api.submitAnswerStream(1, 'synthetic', 0, () => {}, () => done++))
    assert.equal(done, 0); assert.equal(requests(), 1)
  }
})

test('SSE propagates read failures and explicit retry safety', async () => {
  const { api, chunks } = await apiFor()
  chunks.push(new Error('synthetic disconnect'))
  await assert.rejects(api.submitAnswerStream(1, 'synthetic', 0, () => {}, () => {}), /disconnect/)
  for (const safe of [false, true]) {
    const fixture = await apiFor()
    fixture.chunks.push(`data: ${JSON.stringify({ type: 'error', content: 'synthetic', retry_safe: safe })}\n\n`)
    await assert.rejects(fixture.api.submitAnswerStream(1, 'synthetic', 0, () => {}, () => {}),
      error => error.retrySafe === safe)
  }
})

async function viewFor(api = {}) {
  const file = source('ai-interview-frontend/src/views/Interview.vue')
  const script = file.match(/<script setup>([\s\S]*?)<\/script>/)[1]
  const view = await moduleFor(script + '\nexport { handleSubmit, checkSubmission, retrySubmission, renderContent, messages, answer, thinking, submissionUncertain, currentIndex, finished, loadInterviewMetadata };',
    { console: { error() {} } }, {
      vue: { ref, computed: fn => ({ get value() { return fn() } }),
        onMounted() {}, nextTick: fn => fn?.() },
      'vue-router': { useRoute: () => ({ params: { id: 1 } }) },
      '../stores/auth': { useAuthStore: store },
      '../api/interview': { submitAnswerStream: async () => {},
        getMessages: async () => [], getReport: async () => ({}),
        getInterviews: async () => ({ items: [{ interview_id: 1, total_questions: 5, status: 'in_progress' }] }), ...api }
    })
  await view.loadInterviewMetadata()
  return view
}

test('Explicit rollback restores answer and removes optimistic duplicate', async () => {
  let requests = 0
  const view = await viewFor({ submitAnswerStream: async () => {
    requests++; const error = new Error('synthetic failure'); error.retrySafe = true; throw error
  } })
  view.answer.value = 'Synthetic answer'
  await view.handleSubmit()
  assert.equal(view.answer.value, 'Synthetic answer')
  assert.equal(view.submissionUncertain.value, false)
  assert.equal(view.messages.value.filter(m => m.role === 'candidate').length, 0)
  assert.equal(view.thinking.value, false); assert.equal(requests, 1)
})

test('Uncertain empty snapshot blocks resubmission and does not auto retry', async () => {
  let requests = 0
  const view = await viewFor({ submitAnswerStream: async () => { requests++; throw new Error('disconnect') } })
  view.answer.value = 'Synthetic answer'; await view.handleSubmit(); await view.handleSubmit()
  assert.equal(requests, 1); assert.equal(view.submissionUncertain.value, true)
  assert.equal(view.answer.value, 'Synthetic answer')
})

test('Committed answer found after disconnect continues at next question', async () => {
  const rows = [{ role: 'candidate', question_index: 0, content: 'Synthetic answer', score: 0 },
    { role: 'interviewer', question_index: 1, content: 'Q2' }]
  const view = await viewFor({ submitAnswerStream: async () => { throw new Error('disconnect') },
    getMessages: async () => rows })
  view.answer.value = 'Synthetic answer'; await view.handleSubmit()
  assert.equal(view.submissionUncertain.value, false)
  assert.equal(view.currentIndex.value, 1); assert.equal(view.answer.value, '')
  assert.equal(view.messages.value.filter(m => m.role === 'candidate').length, 1)
})

test('Committed final answer is confirmed by report lookup', async () => {
  let reports = 0
  const view = await viewFor({ submitAnswerStream: async () => { throw new Error('disconnect') },
    getMessages: async () => [{ role: 'candidate', question_index: 0, content: 'Synthetic answer', score: 7 }],
    getReport: async () => { reports++; return {} } })
  view.answer.value = 'Synthetic answer'; await view.handleSubmit()
  assert.equal(view.finished.value, true); assert.equal(reports, 1)
})

test('Both answer transports send the explicit expected question identity', async () => {
  const { api, chunks, bodies } = await apiFor()
  await api.submitAnswer(1, 'synthetic', 2)
  chunks.push('data: {"type":"done","question_index":2}\n\n')
  await api.submitAnswerStream(1, 'synthetic', 2, () => {}, () => {})
  assert.equal(bodies.length, 2)
  for (const body of bodies) {
    assert.equal(body.answer, 'synthetic')
    assert.equal(body.expected_question_index, 2)
  }
})

test('Manual retry keeps the original index after an uncertain disconnect', async () => {
  const indices = []
  const view = await viewFor({ submitAnswerStream: async (id, answer, index) => {
    indices.push(index); throw new Error('disconnect')
  } })
  view.currentIndex.value = 1
  view.answer.value = 'Synthetic answer'
  await view.handleSubmit()
  view.currentIndex.value = 2
  await view.retrySubmission()
  assert.deepEqual(indices, [1, 1])
  assert.equal(view.submissionUncertain.value, true)
})

test('Another tab committed this question: reconciliation follows the server answer', async () => {
  const view = await viewFor({ submitAnswerStream: async () => { throw new Error('conflict') },
    getMessages: async () => [{ role: 'candidate', question_index: 0, content: 'Other tab answer', score: 7 },
      { role: 'interviewer', question_index: 1, content: 'Q2' }] })
  view.answer.value = 'Synthetic answer'
  await view.handleSubmit()
  assert.equal(view.submissionUncertain.value, false)
  assert.equal(view.currentIndex.value, 1)
  assert.equal(view.answer.value, '')
})

test('Reconciliation refreshes completion after another tab answered multiple questions', async () => {
  let metadataReads = 0
  let reports = 0
  const view = await viewFor({ submitAnswerStream: async () => { throw new Error('conflict') },
    getInterviews: async () => ({ items: [{ interview_id: 1, total_questions: 3,
      status: ++metadataReads === 1 ? 'in_progress' : 'completed' }] }),
    getMessages: async () => [{ role: 'candidate', question_index: 0, content: 'Other tab', score: 7 },
      { role: 'interviewer', question_index: 1, content: 'Q2' },
      { role: 'interviewer', question_index: 2, content: 'Q3' }],
    getReport: async () => { reports++; return {} } })
  view.answer.value = 'Synthetic answer'
  await view.handleSubmit()
  assert.equal(view.finished.value, true)
  assert.equal(view.submissionUncertain.value, false)
  assert.equal(reports, 1)
})

for (const side of ['ai-interview-admin', 'ai-interview-frontend']) {
  test(`${side} logout calls server before cleanup, with failure notice`, async () => {
    for (const fail of [false, true]) {
      const auth = store(); const calls = []; const alerts = []
      const request = async token => {
        assert.equal(token, 'synthetic-refresh'); assert.equal(auth.token, 'synthetic-access')
        calls.push('server'); if (fail) throw new Error('synthetic network failure')
      }
      auth.logout = () => { calls.push('local'); auth.token = ''; auth.refreshToken = '' }
      const deps = {
        vue: { ref, onMounted() {} },
        './stores/auth': { useAuthStore: () => auth },
        'vue-router': { useRouter: () => ({ push() { calls.push('navigate') } }) },
        './api': { authApi: { logout: request } },
        './api/auth': { logout: request }, './api/user': { getProfile: async () => ({}) }
      }
      const script = source(`${side}/src/App.vue`).match(/<script setup>([\s\S]*?)<\/script>/)[1]
      const app = await moduleFor(script + '\nexport { logout };',
        { window: { alert: text => alerts.push(text) } }, deps)
      await app.logout()
      assert.deepEqual(calls, ['server', 'local', 'navigate'])
      assert.equal(alerts.length, fail ? 1 : 0)
      if (fail) assert.match(alerts[0], /服务器撤销未确认/)
    }
  })
}

test('Admin store persists refresh token and local interceptor cleanup stays local', async () => {
  const values = new Map()
  const storage = { getItem: key => values.get(key), setItem: (key, value) => values.set(key, value),
    removeItem: key => values.delete(key) }
  const module = await moduleFor(source('ai-interview-admin/src/stores/auth.js'), { localStorage: storage }, {
    pinia: { defineStore: (name, fn) => fn }, vue: { ref, computed: fn => fn }
  })
  const auth = module.useAuthStore()
  auth.setAuth({ access_token: 'synthetic-access', refresh_token: 'synthetic-refresh' })
  assert.equal(auth.refreshToken.value, 'synthetic-refresh')
  assert.equal(values.get('admin_refresh_token'), 'synthetic-refresh')
  auth.logout(); assert.equal(values.has('admin_refresh_token'), false)
  for (const side of ['ai-interview-admin', 'ai-interview-frontend']) {
    const request = source(`${side}/src/api/request.js`)
    assert.ok(request.includes('authStore.logout()'))
    assert.ok(!request.includes("'/auth/logout'"))
  }
})

test('Both request clients accept real logout 204 and retain local-only auth cleanup', async () => {
  for (const side of ['ai-interview-admin', 'ai-interview-frontend']) {
    let success, failure, cleaned = 0, posts = 0
    const api = { post() { posts++ }, interceptors: { request: { use() {} },
      response: { use(ok, error) { success = ok; failure = error } } } }
    await moduleFor(source(`${side}/src/api/request.js`), {}, {
      axios: { default: { create: () => api } },
      '../stores/auth': { useAuthStore: () => ({ logout() { cleaned++ } }) },
      '../router': { default: { push() {} } }
    })
    assert.equal(await success({ status: 204, data: '' }), undefined)
    assert.equal(await success({ status: 200, data: { code: 200, data: 'synthetic' } }), 'synthetic')
    await assert.rejects(failure({ response: { status: 401 }, message: 'synthetic' }))
    assert.equal(cleaned, 1); assert.equal(posts, 0)
  }
})

test('Vue files compile; interview HTML is a text VNode, with no innerHTML', async () => {
  assert.ok(process.env.A2_VUE_MODULES, 'Set A2_VUE_MODULES to existing Vue node_modules')
  const modules = process.env.A2_VUE_MODULES
  const compiler = require(path.join(modules, '@vue/compiler-sfc'))
  const vue = require(path.join(modules, 'vue'))
  const attack = '<img src=x onerror="void(0)">\n<b>synthetic text</b>'
  for (const file of ['ai-interview-admin/src/App.vue', 'ai-interview-frontend/src/App.vue',
    'ai-interview-frontend/src/views/Interview.vue']) {
    const parsed = compiler.parse(source(file), { filename: file })
    assert.equal(parsed.errors.length, 0)
    compiler.compileScript(parsed.descriptor, { id: 'a2-fixture' })
    const compiled = compiler.compileTemplate({ source: parsed.descriptor.template.content,
      filename: file, id: 'a2-fixture' })
    assert.equal(compiled.errors.length, 0)
    if (file.endsWith('Interview.vue')) {
      assert.ok(!source(file).includes('v-html'))
      const view = await viewFor()
      assert.equal(view.renderContent(attack), attack)
      const renderer = await moduleFor(compiled.code, {}, { vue })
      const vnode = renderer.render({ preparing: false, finished: false, thinking: false,
        submissionUncertain: false, messages: [{ role: 'candidate', content: attack }],
        renderContent: view.renderContent, userAvatar: '', avatarError: false, userInitial: 'S',
        streamingText: '', currentIndex: 0, totalQuestions: 2, answer: '', handleSubmit() {} }, [])
      let seen = false
      function visit(node) {
        if (!node || typeof node !== 'object') return
        assert.equal(node.props?.innerHTML, undefined)
        if (typeof node.children === 'string' && node.children === attack) seen = true
        if (Array.isArray(node.children)) node.children.forEach(visit)
      }
      visit(vnode); assert.ok(seen, 'Untrusted markup must remain literal text')
    }
  }
})
