const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const vm = require('node:vm');
const source = readFileSync(require('node:path').join(__dirname, '../coding_tools_mcp/chrome_extension/service_worker.js'), 'utf8');

function bridge() {
  let listener;
  const replies = new Map();
  const active = new Set();
  let peak = 0;
  const commands = [];
  const chrome = {
    runtime: {
      connectNative: () => ({
        onMessage: { addListener: fn => { listener = fn; } },
        onDisconnect: { addListener() {} },
        postMessage: reply => { replies.get(reply.id)(reply); replies.delete(reply.id); }
      })
    },
    debugger: {
      async attach({ tabId }) {
        assert.ok(!active.has(tabId), 'concurrent attachment on the same tab');
        active.add(tabId); peak = Math.max(peak, active.size);
      },
      async detach({ tabId }) { active.delete(tabId); },
      async sendCommand(target, method, params) {
        commands.push(params);
        try {
          const result = await vm.runInNewContext(params.expression, { setTimeout, clearTimeout });
          return { result: { value: result } };
        } catch (error) {
          return { exceptionDetails: { text: error.message } };
        }
      }
    }
  };
  vm.runInNewContext(source, { chrome, setTimeout, clearTimeout });
  return {
    commands, active,
    get peak() { return peak; },
    execute(id, script, timeoutMs = 1000) {
      return new Promise(resolve => {
        replies.set(id, resolve);
        listener({ id, action: 'execute', params: { tabId: 1, script, timeoutMs } });
      });
    }
  };
}

test('same-tab executions are serialized and detach after completion', { timeout: 2000 }, async () => {
  const b = bridge();
  const results = await Promise.all([
    b.execute('first', 'new Promise(resolve => setTimeout(() => resolve(1), 30))'),
    b.execute('second', '2')
  ]);
  assert.deepEqual(results.map(x => [x.ok, x.result]), [[true, 1], [true, 2]]);
  assert.equal(b.peak, 1);
  assert.equal(b.active.size, 0);
  assert.ok(b.commands.every(x => x.timeout > 0 && x.timeout <= 1000));
});

test('unresolved evaluation times out and the next request still works', { timeout: 2000 }, async () => {
  const b = bridge();
  const result = await b.execute('timeout', 'new Promise(() => {})', 80);
  assert.equal(result.ok, false);
  assert.match(result.error, /timed out/);
  assert.equal(b.active.size, 0);
  const next = await b.execute('healthy', '42');
  assert.equal(next.ok, true);
  assert.equal(next.result, 42);
});
