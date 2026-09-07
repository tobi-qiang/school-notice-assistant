const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const { test } = require('node:test');
const source = fs.readFileSync(path.join(__dirname, '../app/static/app.js'), 'utf8');
const helpers = source.slice(source.indexOf('let appExiting ='), source.indexOf('// Global delegated handlers'));

function harness(fetch) {
  const elements = new Map();
  const element = key => {
    if (!elements.has(key)) elements.set(key, {
      textContent: '', hidden: true,
      setAttribute() {}, removeAttribute() {}, addEventListener() {},
    });
    return elements.get(key);
  };
  let now = 0, closes = 0;
  const context = vm.createContext({
    document: {querySelector: element, body: {classList: {add() {}}}},
    window: {close() { closes++; }}, fetch,
    AbortController,
    Date: {now: () => now},
    setTimeout(fn, delay) { if (delay === 500) { now += delay; queueMicrotask(fn); } return 1; },
    clearTimeout() {}, setInterval() { return 1; }, clearInterval() {},
    syncPollTimer: null, sendHeartbeat() {}, pollStatus() {},
  });
  vm.runInContext(helpers + '\nthis.exit = requestProgramExit;', context);
  return {run: () => context.exit(), element, closes: () => closes};
}

test('failed exit request never claims success or closes page', async () => {
  const h = harness(async () => { throw new Error('Network reset'); });
  await h.run();
  assert.equal(h.closes(), 0);
  assert.equal(h.element('#exitTitle').textContent, '尚未确认退出');
  assert.equal(h.element('#retryExit').hidden, false);
});

test('accepted exit waits for two failed health checks before closing', async () => {
  let healthCalls = 0;
  const h = harness(async url => {
    if (url === '/api/exit') return {ok: true, json: async () => ({status: 'shutting_down'})};
    healthCalls++;
    throw new Error('Connection refused');
  });
  await h.run();
  assert.equal(healthCalls, 2);
  assert.equal(h.closes(), 1);
  assert.equal(h.element('#exitTitle').textContent, '本地服务已断开');
});

test('accepted request with still-running server reports unconfirmed exit', async () => {
  const h = harness(async url => url === '/api/exit'
    ? {ok: true, json: async () => ({status: 'shutting_down'})}
    : {ok: true});
  await h.run();
  assert.equal(h.closes(), 0);
  assert.equal(h.element('#exitTitle').textContent, '尚未确认退出');
});

test('duplicate tabs get distinct page identities even with copied storage', () => {
  const identity = source.slice(source.indexOf('const heartbeatClientId ='), source.indexOf('const sendHeartbeat ='));
  const values = [1, 2].map(n => vm.runInNewContext(identity + '\nheartbeatClientId;', {
    crypto: {randomUUID: () => `page-${n}`},
    sessionStorage: {getItem: () => 'copied-tab-id'},
  }));
  assert.notEqual(values[0], values[1]);
});
