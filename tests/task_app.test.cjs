"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const html = fs.readFileSync(path.join(__dirname,"../src/webcodex/task_app.html"),"utf8");
const between = (start,end) => html.slice(html.indexOf(start),html.indexOf(end,html.indexOf(start)));
const flush = () => new Promise(resolve => setImmediate(resolve));
function fixture(options={}) {
  const host = {waitId:"wait-one",state:"ready",generation:1,bound:true,autoEnabled:false,
    prepareCalls:0,manualCalls:0,messages:0,finishCalls:[],bindCalls:0,stateCalls:0,takeovers:0,
    failedStates:0,failedFinishes:0,autoCount:0,maximum:3,interval:60,blocked:null,deliveries:[],...options};
  const state = control => ({task:{id:"task",title:"Task",status:host.taskStatus || "active",plan:[]},
    progress:{state:"ready",runner_online:true,liveness:{work:{running_count:host.running || 0,pending_count:0}}},
    continuation:{auto_enabled:host.autoEnabled,wait:host.waitId ? {id:host.waitId,state:host.state} : null,
      controller:{state:host.bound ? "owned" : "other",generation:host.generation},
      policy:{max_auto_messages:host.maximum,min_interval_seconds:host.interval,auto_message_count:host.autoCount,auto_blocked_reason:host.blocked},deliveries:host.deliveries},control});
  const elements={},timers=new Map(); let now=100000,timerId=0;
  function element() { return {dataset:{},children:[],value:"",checked:false,hidden:false,disabled:false,
    addEventListener(name,fn){this[name]=fn;},append(...values){this.children.push(...values);},replaceChildren(...values){this.children=values;},setAttribute(){}}; }
  const context={ready:true,taskId:"task",stopped:false,busy:false,generation:1,viewId:"view",autoConsent:false,
    sending:false,changingPolicy:false,consentRevision:0,manualSequence:0,manualRequest:null,manualWaitId:null,
    last:null,timer:null,pendingFinish:null,failedPrepare:null,pollingBlocked:false,connectionFailed:false,
    selectedPath:null,pendingReview:null,reviewLoaded:false,async loadReview(){},reviewTimer:null,
    nextPollAt:0,retryAt:0,retryDelay:15000,retryLimited:false,scheduleReview(){},
    attempted:new Set(),pending:new Map(),sequence:0,
    document:{hidden:false,activeElement:null,createElement:element},
    Date:class extends Date{static now(){return now;}},
    clearTimeout(id){timers.delete(id);},setTimeout(fn,delay){const id=++timerId;timers.set(id,{fn,delay});return id;},
    el(id){return elements[id] ||= element();},node(tag,text){return Object.assign(element(),{tag,textContent:text});},
    addEventListener(name,fn){if(name==="message")context.onMessage=fn;else context.onVisibility=fn;},
    refreshActivity(){},select(){},applyTheme(){},teardown(){},async initialize(){},
    render(value){context.last=value;context.renderMessaging(value);},
    async call(action,args={}) {
      if(host.beforeCall) await host.beforeCall(action,args,context);
      if(action==="bind") {host.bindCalls++; if(host.bound)host.autoEnabled=false;return state({bound:host.bound,generation:host.generation,reason:host.reason});}
      if(action==="takeover") {host.takeovers++;host.bound=true;host.autoEnabled=false;host.generation++;host.waitId="wait-takeover-"+host.generation;host.state="ready";return state({bound:true,generation:host.generation});}
      if(action==="state") {host.stateCalls++;if(host.failedStates-->0)throw new Error("offline");return state({bound:host.bound,generation:host.generation});}
      if(action==="enable" || action==="pause") {host.autoEnabled=action==="enable";return state({});}
      if(action==="configure") {host.maximum=args.max_auto_messages;host.interval=args.min_interval_seconds;return state({});}
      if(action==="prepare" || action==="prepare_manual") {
        host.prepareCalls++;if(action==="prepare_manual")host.manualCalls++;
        const fail=host.failPrepare;host.failPrepare=null;
        if(fail==="before")throw new Error("prepare not received");
        if(action==="prepare_manual" && host.state==="consumed" && !host.deliveries.some(d=>d.request_id===args.request_id)) {
          host.waitId="manual-wait-"+host.manualCalls;host.state="ready";
        }
        if(host.duplicate || host.deliveries.some(d=>d.wait_id===host.waitId || d.request_id===args.request_id))return state({wait_id:host.waitId,duplicate:true,state:host.state});
        if(action==="prepare") {assert.equal(host.autoEnabled,true);assert.equal(host.blocked,null);host.autoCount++;}
        host.waitId ||= "manual-wait";host.state="prepared";
        host.deliveries.push({wait_id:host.waitId,request_id:args.request_id,mode:action==="prepare"?"auto":"manual",status:"prepared"});
        if(fail==="after")throw new Error("prepared response lost");
        if(host.disableAfterPrepare)context.resetConsent();
        if(host.hideAfterPrepare)context.document.hidden=true;
        return state({wait_id:host.waitId,state:"prepared",app_protocol:{message:host.invalidPrepare?null:"resume"}});
      }
      assert.equal(action,"finish");host.finishCalls.push({...args});
      if(host.failedFinishes-->0)throw new Error("finish unavailable");
      if(!["prepared","accepted","unknown"].includes(host.state))throw new Error("not prepared");
      if(args.outcome==="not_sent") {
        assert.equal(host.state,"prepared");
        const delivery=host.deliveries.find(d=>d.wait_id===host.waitId);
        delivery.status="not_sent";if(delivery.mode==="auto")host.autoCount--;
        host.waitId="wait-retry-"+host.prepareCalls;host.state="watching";
      } else host.state=host.state==="unknown"?"unknown":args.outcome;
      if(host.failedFinishAcks-->0)throw new Error("finish acknowledgement lost");
      return state({state:host.state});
    }};
  context.parent={postMessage(message){
    assert.equal(message.method,"ui/message");host.messages++;
    if(host.messageTimeout){timers.get(context.pending.get(message.id).timeout).fn();return;}
    const reply=host.failMessage ? {error:host.messageError || {code:-32603,message:"Host acknowledgement lost"}}
      :{result:host.failMessageResult?{isError:true,message:"consume_token=private-rejected-token"}:{}};
    context.onMessage({source:context.parent,data:{jsonrpc:"2.0",id:message.id,...reply}});
  }};
  vm.createContext(context);
  vm.runInContext(between("  function deliveryError(","  function payload(")
    +between("  function resetConsent(","  function activityView(")
    +between("  function renderMessaging(","  function render(")
    +between("  function schedule()","  function teardown()")
    +between('  addEventListener("message",','  addEventListener("pagehide",'),context);
  context.render(state({bound:host.bound,generation:host.generation}));
  return {host,context,timers,snapshot:()=>state({bound:host.bound,generation:host.generation}),advance(ms){now+=ms;},
    async enable(){context.el("auto-message").checked=true;await context.el("auto-message").change();},
    async manual(){await context.el("resume").click();},
    async poll(){now=Math.max(now,context.nextPollAt,context.retryAt);await context.poll();}};
}

test("default monitoring reads completed jobs without any prepare or ui/message",async()=>{
  const f=fixture({state:"watching",running:1});await f.poll();f.host.state="ready";f.host.running=0;
  for(let i=0;i<5;i++)await f.poll();
  assert.equal(f.host.prepareCalls,0);assert.equal(f.host.messages,0);assert.equal(f.context.el("auto-message").checked,false);
});
test("manual double click sends exactly once with automatic messaging off",async()=>{
  const f=fixture();await Promise.all([f.manual(),f.manual()]);await f.manual();await f.poll();
  assert.equal(f.host.manualCalls,1);assert.equal(f.host.messages,1);assert.equal(f.host.autoCount,0);
  assert.equal(f.context.el("resume").disabled,true);
});
test("manual continuation can prepare a snapshot without a preexisting wait",async()=>{
  const f=fixture({waitId:null});await f.manual();assert.equal(f.host.messages,1);assert.equal(f.host.manualCalls,1);
});
test("only an explicit checkbox action arms automatic delivery",async()=>{
  const f=fixture();await f.enable();await f.poll();await f.poll();
  assert.equal(f.host.messages,1);assert.equal(f.host.autoCount,1);assert.equal(f.host.manualCalls,0);
});
test("foreign automatic consent is never inherited from a snapshot",async()=>{
  const f=fixture({autoEnabled:true,bound:false});f.context.generation=null;await f.poll();
  assert.equal(f.context.autoConsent,false);assert.equal(f.context.el("auto-message").checked,false);assert.equal(f.host.messages,0);
});
test("reconnect and reopening leave automatic delivery off",async()=>{
  const f=fixture({state:"watching"});await f.enable();await f.context.el("reconnect").click();f.host.state="ready";await f.poll();
  assert.equal(f.context.autoConsent,false);assert.equal(f.host.autoEnabled,false);assert.equal(f.host.messages,0);
  const reopened=fixture({autoEnabled:true});await reopened.poll();assert.equal(reopened.host.messages,0);
});
test("a transport failure revokes local consent and recovery never re-enables it",async()=>{
  const f=fixture({state:"watching"});await f.enable();f.host.failedStates=1;await f.poll();f.host.state="ready";await f.poll();
  assert.equal(f.context.autoConsent,false);assert.equal(f.host.messages,0);assert.equal(f.host.bindCalls,1);
});
test("unchecking after prepare releases its automatic message reservation",async()=>{
  const f=fixture({disableAfterPrepare:true});await f.enable();await f.poll();
  assert.equal(f.host.messages,0);assert.equal(f.host.state,"watching");assert.equal(f.host.autoCount,0);
  assert.equal(f.host.finishCalls[0].outcome,"not_sent");assert.equal(f.host.deliveries[0].status,"not_sent");
  assert.equal(f.context.el("resume").disabled,false);
  f.host.disableAfterPrepare=false;await f.manual();assert.equal(f.host.messages,1);assert.equal(f.host.autoCount,0);
});
test("a hidden card withdraws a prepared message and a lost finish acknowledgement recovers",async()=>{
  const f=fixture({hideAfterPrepare:true,failedFinishAcks:1});await f.manual();
  assert.equal(f.host.messages,0);assert.equal(f.context.pendingFinish.outcome,"not_sent");
  f.host.hideAfterPrepare=false;f.context.document.hidden=false;await f.poll();
  assert.equal(f.context.pendingFinish,null);assert.equal(f.context.el("resume").disabled,false);
  await f.manual();assert.equal(f.host.messages,1);assert.equal(f.host.manualCalls,2);
});
test("manual prepare rechecks a received control revocation before sending",async()=>{
  const f=fixture({beforeCall:async(action,args,context)=>{
    if(action==="prepare_manual") {
      context.last.continuation.controller={state:"other",generation:2};
      context.renderMessaging(context.last);
    }
  }});
  await f.manual();
  assert.equal(f.host.manualCalls,1);assert.equal(f.host.messages,0);
  assert.equal(f.host.finishCalls[0].outcome,"not_sent");
});
test("manual prepare rechecks a received task completion before sending",async()=>{
  const f=fixture({beforeCall:async(action,args,context)=>{
    if(action==="prepare_manual") {
      context.last.task.status="completed";
      context.renderMessaging(context.last);
    }
  }});
  await f.manual();
  assert.equal(f.host.manualCalls,1);assert.equal(f.host.messages,0);
  assert.equal(f.host.finishCalls[0].outcome,"not_sent");
});
test("an invoked automatic message with a lost Host reply keeps its quota reservation",async()=>{
  const f=fixture({messageTimeout:true});await f.enable();await f.poll();await f.poll();
  assert.equal(f.host.messages,1);assert.equal(f.host.autoCount,1);assert.equal(f.host.state,"unknown");
  assert.equal(f.host.finishCalls[0].outcome,"unknown");assert.equal(f.context.el("resume").disabled,true);
});
for(const blocked of ["limit","interval","batching","waiting","disabled"]){
  test(`automatic ${blocked} gate retains results for manual continuation`,async()=>{
    const f=fixture();await f.enable();f.host.blocked=blocked;await f.poll();assert.equal(f.host.messages,0);assert.equal(f.host.prepareCalls,0);
    await f.manual();assert.equal(f.host.messages,1);assert.equal(f.host.autoCount,0);
  });
}
test("server duplicate prepare never calls ui/message",async()=>{
  const f=fixture({duplicate:true});await f.manual();assert.equal(f.host.messages,0);assert.equal(f.host.finishCalls.length,0);
});
test("unknown delivery stays fenced until an explicit takeover",async()=>{
  const f=fixture({failMessage:true});await f.manual();await f.poll();await f.manual();
  assert.equal(f.host.messages,1);assert.equal(f.host.state,"unknown");assert.equal(f.context.el("takeover").hidden,false);
  f.host.failMessage=false;await f.context.el("takeover").click();assert.equal(f.host.messages,1);await f.manual();
  assert.equal(f.host.messages,2);assert.equal(f.host.takeovers,1);assert.equal(f.context.autoConsent,false);
});
test("control takeover is explicit and never sends by itself",async()=>{
  const f=fixture({bound:false});f.context.generation=null;await f.poll();await f.context.el("takeover").click();
  assert.equal(f.host.takeovers,1);assert.equal(f.context.generation,2);assert.equal(f.host.messages,0);assert.equal(f.context.el("resume").disabled,false);
});
test("an observer keeps polling so an expired lease can be recovered",async()=>{
  const f=fixture({bound:false,reason:"another_view"});f.context.generation=null;await f.poll();
  assert.equal(f.host.bindCalls,1);assert.ok(f.timers.size);assert.equal(f.context.el("takeover").hidden,false);
  f.host.bound=true;await f.poll();assert.equal(f.host.bindCalls,2);assert.equal(f.host.messages,0);assert.equal(f.context.generation,1);
});
test("hidden cards never send; returning after reconnect clears consent",async()=>{
  const f=fixture({state:"watching"});await f.enable();f.context.document.hidden=true;f.context.onVisibility();f.host.state="ready";
  await f.poll();assert.equal(f.host.messages,0);f.advance(121000);f.context.document.hidden=false;f.context.onVisibility();await flush();
  assert.equal(f.context.autoConsent,false);assert.equal(f.host.messages,0);
});
test("visible pages never establish a host idle state",()=>{
  assert.doesNotMatch(html,/host(?:Turn)?(?:State)?\s*[:=]\s*["']idle|等模型回覆完|由其他視窗接續/);
});
test("message policy fields validate and save without enabling automatic delivery",async()=>{
  const f=fixture();f.context.el("max-auto-messages").value="4";f.context.el("min-interval").value="120";
  await f.context.el("save-policy").click();assert.equal(f.host.maximum,4);assert.equal(f.host.interval,120);assert.equal(f.context.autoConsent,false);
  f.context.el("max-auto-messages").value="21";await f.context.el("save-policy").click();assert.equal(f.host.maximum,4);assert.match(f.context.el("policy-error").textContent,/0–20/);
});
test("pending finish is retried without retransmitting the user message",async()=>{
  const f=fixture({failedFinishes:1,failMessage:true});await f.manual();assert.equal(f.context.pendingFinish.outcome,"unknown");
  await f.poll();await f.poll();assert.equal(f.context.pendingFinish,null);assert.equal(f.host.messages,1);assert.equal(f.host.finishCalls.length,2);
});
for(const [options,category] of [[{messageTimeout:true},"timeout"],[{failMessageResult:true},"host_rejected"],[{invalidPrepare:true},"invalid_prepare"]]){
  test(`${category} is saved once and never auto-retried`,async()=>{
    const f=fixture(options);await f.manual();await f.poll();await f.poll();
    assert.equal(f.host.state,"unknown");assert.equal(f.host.messages,category==="invalid_prepare"?0:1);
    assert.equal(f.host.finishCalls[0].delivery_error,category);assert.doesNotMatch(JSON.stringify(f.host.finishCalls),/consume_token|private-rejected-token/);
  });
}
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


test("runner state, work observations, tool activity and control ownership are separate",()=>{
  for(const label of ["Runner 連線","工作狀態","本對話工具呼叫","卡片控制權","本任務自動發訊次數"])assert.ok(html.includes(label));
  const f=fixture();vm.runInContext(between("  function activityView(","  function renderHandoff("),f.context);
  const now=1000;f.context.activityReceivedAt=f.context.Date.now();f.context.stateCopy=["沒有執行中的背景工作",""];
  f.context.last.progress.liveness={observed_at:now,runner_last_seen:now,host_turn_state:"unknown",data_freshness:"live",
    operation:"讀取檔案",conversation:{outcome:"returned",started_at:850,finished_at:900},work:{running_count:0,pending_count:0}};
  f.context.refreshActivity();assert.match(f.context.el("runner-activity").textContent,/已連線/);assert.match(f.context.el("work-observation").textContent,/沒有執行中/);
  f.context.connectionFailed=true;f.context.refreshActivity();assert.match(f.context.el("conversation-activity").textContent,/舊紀錄/);assert.match(f.context.el("work-observation").textContent,/舊紀錄/);
  f.context.connectionFailed=false;f.context.last.continuation.controller={state:"other",generation:2,updated_at:900,lease_until:999};f.context.refreshActivity();
  assert.equal(f.context.el("controller-state").textContent,"控制權已到期");assert.match(f.context.el("controller-updated").textContent,/最後更新/);
});

test("conversation call time stays at request start despite completion and newer task updates",()=>{
  const f=fixture();vm.runInContext(between("  function activityView(","  function renderHandoff("),f.context);
  const value=f.snapshot();value.progress.last_job_update=999;
  value.progress.liveness={observed_at:1000,operation:"讀取檔案",conversation:{outcome:"running",started_at:700,finished_at:null}};
  const started=f.context.timestamp(700);
  assert.equal(f.context.activityView(value).conversation,`${started}（5 分鐘前） · 讀取檔案 · 工具等候回應`);
  value.progress.liveness.conversation={outcome:"returned",started_at:700,finished_at:995};
  value.progress.last_job_update=1000;
  assert.equal(f.context.activityView(value).conversation,`${started}（5 分鐘前） · 讀取檔案 · 工具已回傳`);
  value.progress.liveness.observed_at=1060;
  assert.equal(f.context.activityView(value).conversation,`${started}（6 分鐘前） · 讀取檔案 · 工具已回傳`);
});

test("missing conversation call never falls back to a task update or return time",()=>{
  const f=fixture();vm.runInContext(between("  function activityView(","  function renderHandoff("),f.context);
  const value=f.snapshot();value.progress.last_job_update=999;
  value.progress.liveness={observed_at:1000,conversation:null};
  assert.equal(f.context.activityView(value).conversation,"尚無紀錄");
  value.progress.liveness.conversation={outcome:"returned",finished_at:998};
  assert.equal(f.context.activityView(value).conversation,"尚無紀錄");
});

test("a consumed tool-result resets the manual click id before the next poll",async()=>{
  const f=fixture();await f.manual();const firstRequest=f.host.deliveries[0].request_id;
  f.host.state="consumed";f.context.render(f.snapshot());
  assert.equal(f.context.manualRequest,null);assert.equal(f.context.el("resume").disabled,false);
  await f.manual();assert.equal(f.host.messages,2);assert.equal(f.host.manualCalls,2);
  assert.notEqual(f.host.deliveries[1].request_id,firstRequest);
});
test("handoff errors display tool, state and process without object coercion",()=>{
  const f=fixture();vm.runInContext(between("  function renderHandoff(","  function renderMessaging("),f.context);
  f.context.renderHandoff({issues:[{tool:"exec_command",status:"failed",exit_code:2,process_id:"original-process"},"Check fixture"]});
  const list=f.context.el("handoff-content").children.at(-1);
  assert.equal(list.children[0].textContent,"exec_command · 失敗 · 結束碼 2 · original-process");
  assert.equal(list.children[1].textContent,"Check fixture");
});

test("a new wait resets the click id even if its consumed predecessor was not observed",async()=>{
  const f=fixture();await f.manual();const firstRequest=f.host.deliveries[0].request_id;
  f.host.waitId="new-wait";f.host.state="ready";f.context.render(f.snapshot());
  assert.equal(f.context.manualRequest,null);await f.manual();assert.equal(f.host.messages,2);
  assert.notEqual(f.host.deliveries[1].request_id,firstRequest);
});
