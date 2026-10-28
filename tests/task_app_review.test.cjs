"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const html = fs.readFileSync(path.join(__dirname, "../src/webcodex/task_app.html"), "utf8");
const between = (start, end) => html.slice(html.indexOf(start), html.indexOf(end, html.indexOf(start)));
const flush = () => new Promise(resolve => setImmediate(resolve));

function fixture(replies) {
  let now = 100000, nextId = 0;
  const elements = {}, timers = new Map(), calls = [], shown = [];
  const context = {
    ready: true, taskId: "task", stopped: false, busy: false, generation: 1, pollingBlocked: false,
    sequence: 0, pending: new Map(), viewId: "view", reviewVersion: 0, reviewLoaded: false,
    selectedPath: null, pendingReview: null, reviewTimer: null, timer: null,
    nextPollAt: 0, retryAt: 0, retryDelay: 15000, retryLimited: false,
    document: {hidden: false}, Date: class extends Date { static now() { return now; } },
    setTimeout(fn, delay) { const id = ++nextId; timers.set(id,{fn,at:now+delay,delay}); return id; },
    clearTimeout(id) { timers.delete(id); },
    el(id) { return elements[id] ||= {dataset: {}, attributes: {},
      setAttribute(key,value) { this.attributes[key] = value; },
      addEventListener(event,fn) { this[event] = fn; }}; },
    showReview(review) { shown.push(review); context.el("review-state").dataset.loaded = "true"; },
    addEventListener(event,fn) { if (event === "message") context.onMessage = fn; else context.onVisibility = fn; },
    poll() {}, select() {}, render() {}, applyTheme() {},
  };
  context.parent = {postMessage(message) {
    assert.equal(message.method,"tools/call");
    calls.push(message.params);
    const reply = replies.shift();
    assert.ok(reply, "unexpected Host request");
    if (reply.drop) return;
    context.onMessage({source:context.parent,data:{jsonrpc:"2.0",id:message.id,
      ...(reply.error ? {error:reply.error} : {result:reply.mcp || {structuredContent:reply}})}});
  }};
  vm.createContext(context);
  vm.runInContext(between("  function deliveryError(","  async function call(")
    + between("  function scheduleReview(","  function selectTab(")
    + between('  addEventListener("message",','  addEventListener("pagehide",'),context);
  return {context,calls,shown,timers,
    async advance(ms) {
      now += ms;
      for (const [id,item] of Array.from(timers)) if (item.at <= now) {
        timers.delete(id); item.fn(); await flush();
      }
    },
    retryDelay() { return timers.get(context.reviewTimer)?.at-now; },
  };
}

const completed = {task_id:"task",review:{files:[],executions:[{process_id:"original",status:"completed"}]}};
const running = {task_id:"task",review_job:{job_id:"original",status:"running",runner_online:true}};

test("rate limiting releases the loading state, honors retry-after, and recovers the same request", async () => {
  const f = fixture([{error:{code:-32000,message:"Too many requests",data:{retry_after:45}}},completed]);
  await f.context.loadReview();
  assert.equal(f.context.generation,1,"a review error must preserve the state controller");
  assert.match(f.context.el("review-state").textContent,/請求暫時受限.*自動重試/);
  assert.equal(f.context.el("review-refresh").disabled,false);
  assert.equal(f.context.el("changes").attributes["aria-busy"],"false");
  assert.equal(f.retryDelay(),45000);
  const requestId = f.calls[0].arguments.request_id;
  await f.context.el("review-refresh").click();
  assert.equal(f.calls.length,1,"manual retry must respect the existing cooldown");
  await f.advance(45000);
  assert.equal(f.calls.length,2);
  assert.equal(f.calls[1].arguments.request_id,requestId);
  assert.equal(f.shown.length,1);
  assert.equal(f.context.pendingReview,null);
  assert.equal(f.context.el("review-refresh").textContent,"重新整理");
  assert.equal(f.timers.size,0);
});

test("a lost first response retries with the same request id instead of another scan", async () => {
  const f = fixture([{drop:true},completed]);
  const loading = f.context.loadReview("README.md");
  await f.advance(10000); await loading;
  assert.equal(f.context.el("review-refresh").disabled,false);
  assert.match(f.context.el("review-state").textContent,/自動重試/);
  await f.advance(15000);
  assert.equal(f.calls[1].arguments.request_id,f.calls[0].arguments.request_id);
  assert.equal(f.calls[1].arguments.path,"README.md");
  assert.equal(f.shown.length,1);
});

test("a pending review survives a timeout and keeps the exact job on recovery", async () => {
  const f = fixture([running,{drop:true},completed]);
  await f.context.loadReview();
  assert.equal(f.retryDelay(),15000);
  await f.advance(15000);
  assert.equal(f.calls[1].arguments.job_id,"original");
  await f.advance(10000);
  await f.advance(15000);
  assert.equal(f.calls[2].arguments.job_id,"original");
  assert.equal(f.calls[2].arguments.request_id,f.calls[0].arguments.request_id);
  assert.equal(f.shown.length,1);
});

test("repeated failures back off and keep the previous rendered result", async () => {
  const f = fixture([{error:{code:429,message:"rate_limit_exceeded"}},
    {error:{code:429,message:"rate_limit_exceeded"}},completed]);
  f.context.el("review-state").dataset.loaded = "true";
  await f.context.loadReview();
  assert.match(f.context.el("review-state").textContent,/保留上次結果/);
  await f.advance(15000);
  assert.equal(f.retryDelay(),30000);
  assert.equal(f.shown.length,0);
  await f.advance(30000);
  assert.equal(f.shown.length,1);
});

test("a rate limit returned as an MCP tool error also recovers", async () => {
  const f = fixture([{mcp:{isError:true,content:[{type:"text",text:"Too many requests"}]}},completed]);
  await f.context.loadReview();
  assert.match(f.context.el("review-state").textContent,/請求暫時受限/);
  assert.equal(f.retryDelay(),15000);
  await f.advance(15000);
  assert.equal(f.shown.length,1);
});

test("background cards stop review retries and resume the original job when visible", async () => {
  const f = fixture([running,completed]);
  await f.context.loadReview();
  f.context.document.hidden = true; f.context.onVisibility();
  assert.equal(f.timers.size,0);
  await f.advance(60000);
  assert.equal(f.calls.length,1);
  f.context.document.hidden = false; f.context.onVisibility();
  assert.equal(f.retryDelay(),15000);
  await f.advance(14999);
  assert.equal(f.calls.length,1);
  await f.advance(1);
  assert.equal(f.calls[1].arguments.job_id,"original");
  assert.equal(f.shown.length,1);
});
