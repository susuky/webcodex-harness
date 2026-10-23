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
const requestStart = html.indexOf("  function deliveryError(");
const requestEnd = html.indexOf("  function payload(", requestStart);
const messageStart = html.indexOf('  addEventListener("message",');
const messageEnd = html.indexOf('  addEventListener("visibilitychange",', messageStart);
assert.ok(requestStart >= 0 && requestEnd > requestStart && messageStart >= 0 && messageEnd > messageStart);
const requestSource = html.slice(requestStart, requestEnd);
const messageSource = html.slice(messageStart, messageEnd);
const manualSource = html.slice(html.indexOf('  el("check-status").addEventListener'), html.indexOf("  function teardown()"));
const visibilitySource = html.slice(messageEnd, html.indexOf('  addEventListener("pagehide",', messageEnd));

function fixture(options = {}) {
  const host = {
    waitId: "wait-one", state: "ready", generation: 1, bound: true, paused: false,
    prepareCalls: 0, messages: 0, failedStates: 0, failedFinishes: 0, finishCalls: [], bindCalls: 0,
    failPrepare: "before", ...options,
  };
  const state = control => ({
    task: {id: "task", status: "active"},
    continuation: {paused: host.paused, wait: {id: host.waitId, state: host.state}},
    control,
  });
  const elements = {};
  const timers = new Map();
  let timerId = 0;
  const context = {
    ready: true, taskId: "task", stopped: false, busy: false, generation: 1,
    paused: false, last: null, timer: null, pendingFinish: null, failedPrepare: null, pollingBlocked: false,
    selectedPath: null, pendingReview: null, async loadReview() {},
    attempted: new Set(), pending: new Map(), sequence: 0, document: {hidden: false},
    clearTimeout(id) { timers.delete(id); },
    setTimeout(callback) { const id = ++timerId; timers.set(id, callback); return id; },
    addEventListener(name, listener) {
      if (name === "message") context.onMessage = listener;
      else { assert.equal(name, "visibilitychange"); context.onVisibility = listener; }
    },
    el(id) { return elements[id] ||= {addEventListener(name, listener) { this[name] = listener; }}; },
    refreshActivity() {},
    render(value) { context.last = value; context.paused = value.continuation.paused; },
    async call(action, args = {}) {
      if (action === "bind") {
        host.bindCalls++;
        return state({bound: host.bound, generation: host.generation, reason: host.reason});
      }
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
        if (host.pauseAfterPrepare) context.paused = true;
        return state({wait_id: args.wait_id, state: "prepared", app_protocol: {message: host.invalidPrepare ? null : "resume"}});
      }
      assert.equal(action, "finish");
      host.finishCalls.push({...args});
      if (host.failedFinishes > 0) { host.failedFinishes--; throw new Error("finish unavailable"); }
      if (host.state === "ready") throw new Error("wait was not prepared");
      host.state = host.state === "unknown" ? "unknown" : args.outcome;
      return state({state: host.state});
    },
  };
  context.parent = {
    postMessage(message) {
      assert.equal(message.method, "ui/message"); host.messages++;
      if (host.messageTimeout) {
        timers.get(context.pending.get(message.id).timeout)();
        return;
      }
      const response = host.failMessage
        ? {error: host.messageError || {code: -32603, message: "Host acknowledgement lost"}}
        : {result: host.failMessageResult ? {isError: true, message: "consume_token=private-rejected-token"} : {}};
      context.onMessage({source: context.parent, data: {jsonrpc: "2.0", id: message.id, ...response}});
    },
  };
  vm.createContext(context);
  vm.runInContext(requestSource + messageSource, context);
  vm.runInContext(source + manualSource + visibilitySource, context);
  return {host, context, timers, poll: () => context.poll()};
}

for (const reason of ["missing_window", "read_only", "replaced_view", "another_view"]) {
  test(`an unbound ${reason} card stops automatic polling until a manual reconnect`, async () => {
    const {host, context, timers, poll} = fixture({bound: false, reason, failPrepare: null});
    context.generation = null;
    await poll();
    assert.equal(host.bindCalls, 1);
    assert.equal(context.pollingBlocked, true);
    assert.equal(timers.size, 0);
    assert.equal(context.el("reconnect").hidden, false);
    context.onVisibility(); await poll();
    assert.equal(host.bindCalls, 1);
    assert.equal(host.messages, 0);
    host.bound = true;
    await context.el("reconnect").click();
    assert.equal(host.bindCalls, 2);
    assert.equal(context.pollingBlocked, false);
    assert.equal(host.messages, 1);
    assert.equal(timers.size, 1);
  });
}

test("losing an existing controller stops polling and permits a manual status check", async () => {
  const {host, context, timers, poll} = fixture({failPrepare: null});
  await poll();
  host.bound = false;
  await poll();
  assert.equal(context.generation, null);
  assert.equal(timers.size, 0);
  context.document.hidden = true; context.onVisibility();
  context.document.hidden = false; context.onVisibility();
  assert.equal(host.bindCalls, 0);
  host.bound = true;
  await context.el("check-status").click();
  assert.equal(host.bindCalls, 1);
  assert.equal(timers.size, 1);
  assert.equal(host.messages, 1);
});

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
  assert.equal(context.el("connection").textContent, "卡片重新連線中");
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

const activityStart = html.indexOf("  function activityView(");
const activityEnd = html.indexOf("  function render(", activityStart);
assert.ok(activityStart >= 0 && activityEnd > activityStart);
const activitySource = html.slice(activityStart, activityEnd);
function activityFixture() {
  const now = 1800000000;
  const value = {
    task: {status: "active"},
    progress: {
      state: "observing", runner_online: true, pending_jobs: [], process_observations: [],
      liveness: {observed_at: now, runner_last_seen: now - 1,
        work: {running_count: 0, pending_count: 0, observed_at: null},
        conversation: {started_at: now - 722, finished_at: now - 720, outcome: "returned"}},
    },
    continuation: {paused: false, wait: null},
  };
  const elements = {};
  const context = {last: value, activityReceivedAt: 1000000, connectionFailed: false,
    stateCopy: ["工作進行中", ""], Date: class extends Date { static now() { return 1000000; } },
    el(id) { return elements[id] ||= {dataset: {}}; }};
  vm.createContext(context);
  vm.runInContext(activitySource, context);
  return {value, context, view: (elapsed=0) => context.activityView(value, elapsed)};
}

test("a live card and Runner do not turn old conversation activity into a live-model claim", () => {
  const {view} = activityFixture();
  const result = view();
  assert.match(result.conversation, /最近工具回應.*12 分鐘前/);
  assert.match(result.runner, /已連線/);
  assert.match(result.notice, /12 分鐘沒有新的對話工具活動/);
});

test("quiet warnings respect registered waits, explicit blockers, completion and pause", () => {
  const {value, view} = activityFixture();
  for (const state of ["watching", "ready", "prepared", "accepted", "unknown"]) {
    value.continuation.wait = {state};
    assert.equal(view().notice, null);
  }
  value.continuation.wait = null;
  for (const state of ["waiting_runner", "waiting_operator", "needs_attention", "completed"]) {
    value.progress.state = state;
    assert.equal(view().notice, null);
  }
  value.progress.state = "observing"; value.continuation.paused = true;
  assert.equal(view().notice, null);
  value.continuation.paused = false; value.task.status = "completed";
  assert.equal(view().notice, null);
});

test("activity ages use the server observation plus elapsed time and preserve missing data", () => {
  const {value, view} = activityFixture();
  value.progress.liveness.conversation.finished_at = value.progress.liveness.observed_at - 179;
  assert.equal(view().notice, null);
  assert.match(view(1).notice, /3 分鐘/);
  value.progress.liveness.conversation = null;
  assert.equal(view().conversation, "尚未取得工具活動紀錄");
  assert.equal(view(10000).notice, null);
  delete value.progress.liveness;
  assert.equal(view().conversation, "尚未取得工具活動紀錄");
});

test("an in-flight request or old process observation is identified by its own timestamp", () => {
  const {value, view} = activityFixture();
  value.progress.liveness.conversation.outcome = "running";
  value.progress.liveness.conversation.finished_at = null;
  value.progress.liveness.work = {running_count: 1, pending_count: 0, observed_at: value.progress.liveness.observed_at - 600};
  assert.match(view().conversation, /工具等候回應/);
  assert.match(view().notice, /工具回應已等候/);
  assert.equal(view().work, "上次觀察有 1 項工作執行中（10 分鐘前）");
});

test("failed polling and stale card data cannot keep showing a green connection", () => {
  const {context} = activityFixture();
  context.connectionFailed = true;
  context.refreshActivity();
  assert.equal(context.el("connection").textContent, "卡片重新連線中");
  assert.equal(context.el("runner-activity").textContent, "尚無最新連線狀態");
  assert.equal(context.el("reconnect").hidden, false);
  context.connectionFailed = false;
  context.activityReceivedAt -= 45000;
  context.refreshActivity();
  assert.equal(context.el("status").textContent, "卡片資料待更新");
  context.activityReceivedAt = context.Date.now();
  context.refreshActivity();
  assert.match(context.el("status").textContent, /沒有新的對話工具活動/);
  assert.match(context.el("runner-activity").textContent, /已連線/);
});

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

test("Host RPC errors retain only a fixed category and a valid JSON-RPC code", async () => {
  const {context} = fixture({
    failMessage: true,
    messageError: {code: -32042, message: "consume_token=private-rpc-token", data: {consume_token: "private-rpc-token"}},
  });
  await assert.rejects(context.request("ui/message"), error => {
    assert.equal(error.message, "rpc_error");
    assert.equal(error.delivery_error, "rpc_error");
    assert.equal(error.delivery_error_code, -32042);
    assert.deepEqual(Object.keys(error).sort(), ["delivery_error", "delivery_error_code"]);
    assert.doesNotMatch(JSON.stringify(error), /consume_token|private-rpc-token/);
    return true;
  });
});

test("invalid or out-of-range Host RPC codes are omitted", async () => {
  for (const code of [undefined, "-32000", 1.5, NaN, Infinity, Number.MAX_SAFE_INTEGER, -(2**31) - 1, 2**31]) {
    const {context} = fixture({failMessage: true, messageError: {code, message: "private"}});
    await assert.rejects(context.request("ui/message"), error => {
      assert.equal(error.delivery_error, "rpc_error");
      assert.equal(Object.hasOwn(error, "delivery_error_code"), false);
      return true;
    });
  }
  for (const code of [-(2**31), 2**31 - 1]) {
    const {context} = fixture({failMessage: true, messageError: {code}});
    await assert.rejects(context.request("ui/message"), error => error.delivery_error_code === code);
  }
});

test("an uncertain RPC delivery retries finish with the same safe metadata and sends once", async () => {
  const {host, context, poll} = fixture({
    failPrepare: null, failMessage: true, failedFinishes: 1,
    messageError: {code: -32042, message: "consume_token=private-rpc-token", data: {consume_token: "private-rpc-token"}},
  });
  await poll();
  assert.equal(host.state, "prepared");
  assert.equal(context.pendingFinish.delivery_error, "rpc_error");
  assert.equal(context.pendingFinish.delivery_error_code, -32042);
  assert.doesNotMatch(JSON.stringify(context.pendingFinish), /consume_token|private-rpc-token|message|data/);
  await poll(); await poll();
  assert.equal(host.state, "unknown");
  assert.equal(host.messages, 1); assert.equal(host.prepareCalls, 1);
  assert.equal(context.pendingFinish, null);
  assert.deepEqual(host.finishCalls, [
    {wait_id: "wait-one", outcome: "unknown", delivery_error: "rpc_error", delivery_error_code: -32042},
    {wait_id: "wait-one", outcome: "unknown", delivery_error: "rpc_error", delivery_error_code: -32042},
  ]);
});

for (const [options, category] of [
  [{messageTimeout: true}, "timeout"],
  [{failMessageResult: true}, "host_rejected"],
  [{pauseAfterPrepare: true}, "interrupted"],
  [{invalidPrepare: true}, "invalid_prepare"],
]) {
  test(`${category} is recorded without retaining Host content or resending`, async () => {
    const {host, poll} = fixture({failPrepare: null, ...options});
    await poll(); await poll();
    assert.equal(host.state, "unknown");
    assert.equal(host.prepareCalls, 1);
    assert.equal(host.messages, ["timeout", "host_rejected"].includes(category) ? 1 : 0);
    assert.deepEqual(host.finishCalls, [{wait_id: "wait-one", outcome: "unknown", delivery_error: category}]);
    assert.doesNotMatch(JSON.stringify(host.finishCalls), /consume_token|private-rejected-token|message|data/);
  });
}

for (const change of ["wait", "generation"]) {
  test(`a stale pending finish cannot carry diagnostics into a new ${change}`, async () => {
    const {host, context, poll} = fixture({failPrepare: null, messageTimeout: true, failedFinishes: 1});
    await poll();
    assert.equal(context.pendingFinish.delivery_error, "timeout");
    host.messageTimeout = false;
    if (change === "wait") {
      host.waitId = "wait-two"; host.state = "ready";
    } else {
      context.generation = null; host.generation++;
    }
    await poll(); await poll();
    assert.equal(context.pendingFinish, null);
    assert.deepEqual(host.finishCalls[1], change === "wait"
      ? {wait_id: "wait-two", outcome: "accepted"}
      : {wait_id: "wait-one", outcome: "unknown", delivery_error: "interrupted"});
    assert.equal(host.messages, change === "wait" ? 2 : 1);
  });
}
