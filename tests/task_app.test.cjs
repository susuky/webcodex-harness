"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const html = fs.readFileSync(path.join(__dirname, "../src/webcodex/task_app.html"), "utf8");
const start = html.indexOf("  function schedule()");
const end = html.indexOf('  el("pause").addEventListener', start);
assert.ok(start >= 0 && end > start);
const source = html.slice(start, end);

function fixture(options = {}) {
  const host = {
    waitId: "wait-one", state: "ready", generation: 1, bound: true, paused: false,
    prepareCalls: 0, messages: 0, failedStates: 0, failedFinishes: 0,
    failPrepare: "before", ...options,
  };
  const state = control => ({
    task: {id: "task", status: "active"},
    continuation: {paused: host.paused, wait: {id: host.waitId, state: host.state}},
    control,
  });
  const elements = {};
  const context = {
    ready: true, taskId: "task", stopped: false, busy: false, generation: 1,
    paused: false, last: null, timer: null, pendingFinish: null, failedPrepare: null,
    attempted: new Set(), document: {hidden: false},
    clearTimeout() {}, setTimeout() { return 1; },
    el(id) { return elements[id] ||= {}; },
    render(value) { context.last = value; context.paused = value.continuation.paused; },
    async call(action, args = {}) {
      if (action === "bind") return state({bound: true, generation: host.generation});
      if (action === "state") {
        if (host.failedStates > 0) { host.failedStates--; throw new Error("state unavailable"); }
        return state({bound: host.bound, generation: host.generation});
      }
      if (action === "prepare") {
        host.prepareCalls++;
        const fail = host.failPrepare; host.failPrepare = null;
        if (fail === "before") throw new Error("request never reached Server");
        assert.equal(host.state, "ready");
        host.state = "prepared";
        if (fail === "after") throw new Error("prepared response lost");
        return state({wait_id: args.wait_id, state: "prepared", app_protocol: {message: "resume"}});
      }
      assert.equal(action, "finish");
      if (host.failedFinishes > 0) { host.failedFinishes--; throw new Error("finish unavailable"); }
      if (host.state === "ready") throw new Error("wait was not prepared");
      host.state = host.state === "unknown" ? "unknown" : args.outcome;
      return state({state: host.state});
    },
    async request(method) {
      assert.equal(method, "ui/message"); host.messages++;
      if (host.failMessage) throw new Error("Host acknowledgement lost");
      return host.failMessageResult ? {isError: true} : {};
    },
  };
  vm.createContext(context);
  vm.runInContext(source, context);
  return {host, context, poll: () => context.poll()};
}

test("a prepare that never reached Server recovers after confirmed same-controller ready state", async () => {
  const {host, context, poll} = fixture();
  await poll();
  assert.equal(host.state, "ready"); assert.equal(host.messages, 0);
  assert.ok(context.attempted.has(host.waitId));
  await poll(); await poll();
  assert.equal(host.prepareCalls, 2); assert.equal(host.messages, 1);
  assert.equal(host.state, "accepted");
});

test("a failed state query can recover on a later poll with the same generation", async () => {
  const {host, context, poll} = fixture();
  await poll(); host.failedStates = 1;
  await poll(); assert.equal(context.generation, null);
  await poll();
  assert.equal(host.prepareCalls, 2); assert.equal(host.messages, 1);
});

test("reconnect appears after polling fails and clears without resending an uncertain message", async () => {
  const {host, context, poll} = fixture({failPrepare: null, failMessage: true});
  await poll();
  assert.equal(host.state, "unknown");
  assert.equal(context.el("reconnect").hidden, true);
  host.failedStates = 1;
  await poll();
  assert.equal(context.el("reconnect").hidden, false);
  assert.equal(context.el("connection").textContent, "重新連線中");
  await poll();
  assert.equal(context.el("reconnect").hidden, true);
  assert.equal(host.state, "unknown");
  assert.equal(host.prepareCalls, 1);
  assert.equal(host.messages, 1);
});

test("a lost prepared response never causes another prepare or Host message", async () => {
  const {host, poll} = fixture({failPrepare: "after"});
  await poll(); await poll();
  assert.equal(host.state, "unknown");
  assert.equal(host.prepareCalls, 1); assert.equal(host.messages, 0);
});

for (const waitState of ["prepared", "accepted", "unknown"]) {
  test(`an observed ${waitState} fence is never made sendable`, async () => {
    const {host, poll} = fixture({failPrepare: "after", failedFinishes: 1});
    await poll(); host.state = waitState;
    await poll(); await poll();
    assert.equal(host.prepareCalls, 1); assert.equal(host.messages, 0);
  });
}

test("ready from an unbound controller does not clear the attempted fence", async () => {
  const {host, context, poll} = fixture();
  await poll(); host.bound = false;
  await poll();
  assert.ok(context.attempted.has(host.waitId));
  assert.equal(host.prepareCalls, 1); assert.equal(host.messages, 0);
});

test("ready after a controller generation change does not replay the failed attempt", async () => {
  const {host, context, poll} = fixture();
  await poll(); host.failedStates = 1;
  await poll(); host.generation++;
  await poll();
  assert.equal(context.generation, 2);
  assert.ok(context.attempted.has(host.waitId));
  assert.equal(host.prepareCalls, 1); assert.equal(host.messages, 0);
});

test("a new wait cannot clear the old wait's attempted fence", async () => {
  const {host, context, poll} = fixture();
  await poll(); host.waitId = "wait-two";
  await poll();
  assert.ok(context.attempted.has("wait-one"));
  assert.equal(host.prepareCalls, 2); assert.equal(host.messages, 1);
});

test("a lost Host message acknowledgement never resends the message", async () => {
  const {host, poll} = fixture({failPrepare: null, failMessage: true});
  await poll(); await poll();
  assert.equal(host.state, "unknown");
  assert.equal(host.prepareCalls, 1); assert.equal(host.messages, 1);
});

test("a rejected Host message result never becomes accepted or resends the message", async () => {
  const {host, poll} = fixture({failPrepare: null, failMessageResult: true});
  await poll();
  assert.equal(host.state, "unknown");
  host.failMessageResult = false;
  await poll(); await poll();
  assert.equal(host.state, "unknown");
  assert.equal(host.prepareCalls, 1); assert.equal(host.messages, 1);
});
