// Offline stream regression: arbitrary chunk boundaries, UTF-8, close and reconnect.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

async function main() {
  // Completion navigations are one-shot; all repeated reads carry the marker.
  for (const kind of ['job', 'queued']) {
    const timers = [], navigations = [], calls = [];
    const node = { dataset: { queuedRun: '12' }, querySelector: () => null };
    const location = {
      href: 'http://localhost/documents/1?job=12',
      replace: url => navigations.push(url), reload: () => navigations.push('reload')
    };
    const context = {
      URL, window: { location, setTimeout: cb => timers.push(cb) },
      setTimeout: cb => timers.push(cb),
      document: { visibilityState: 'visible', querySelector: () => node },
      fetch: async (url, init) => {
        calls.push(init);
        return { ok: true, json: async () => ({ status: 'completed' }) };
      }
    };
    vm.createContext(context);
    vm.runInContext(fs.readFileSync('app/static/' + (kind === 'job' ? 'jobs' : 'queuedrun') + '.js', 'utf8'), context);
    if (kind === 'job') context.watchJob(12, node);
    else await timers.shift()();
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(navigations.length, 1, kind);
    assert.equal(timers.length, 0, kind);
    assert.equal(calls[0].headers['X-Loc-Background'], '1');
    if (kind === 'job') assert.equal(navigations[0], 'http://localhost/documents/1');
  }
  const batch = fs.readFileSync('app/templates/batch_view.html', 'utf8');
  const reload = batch.match(/function boundedReload\(\) \{[\s\S]*?\n  \}/)[0];
  const storage = new Map();
  let reloads = 0;
  const contextForPage = () => ({
    BATCH_RELOAD_MAX: Number(batch.match(/BATCH_RELOAD_MAX = (\d+)/)[1]), reloadKey: 'batch-12',
    sessionStorage: { getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value) },
    window: { location: { reload: () => reloads++ } }
  });
  for (let page = 0; page < 3; page++) {
    const context = contextForPage();
    vm.createContext(context);
    vm.runInContext(reload + '; boundedReload();', context);
  }
  assert.equal(reloads, 1, 'batch reload cap survives navigation');

  const calls = [], timers = [], messages = [];
  let closed = 0;
  const bytes = new TextEncoder().encode('data: first\r\ndata: second\r\n\r\ndata: caf\u00e9\n\n');
  const context = {
    TextDecoder, AbortController,
    setTimeout: (callback) => timers.push(callback),
    fetch: async (url, init) => {
      calls.push(init);
      init.signal.addEventListener('abort', () => { closed++; });
      let index = 0;
      return {
        ok: true, headers: new Headers({ 'content-type': 'text/event-stream' }),
        body: { getReader: () => ({ read: async () => index < bytes.length
          ? { value: bytes.slice(index, ++index), done: false } : { done: true } }) }
      };
    }
  };
  vm.createContext(context);
  vm.runInContext(fs.readFileSync('app/static/sse.js', 'utf8'), context);
  const handle = context.connectSSE('/stream', { onMessage: e => messages.push(e.data) });
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(messages, ['first\nsecond', 'caf\u00e9']);
  assert.equal(calls[0].headers['X-Loc-Background'], '1');
  assert.equal(timers.length, 1);
  assert.equal(closed, 1);
  timers.shift()();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(calls.length, 2);
  handle.close();
  timers.shift()();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(calls.length, 2);
  console.log('BACKGROUND STREAM OK');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
