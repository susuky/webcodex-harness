"use strict";
const assert=require("node:assert/strict"),fs=require("node:fs"),os=require("node:os"),path=require("node:path");
async function verifyConnections(browser,root,output,checks,errors,measurements,{baseline=false}={}){
  const now=1794817200;
  measurements.connections=[];
  for(const width of [1400,390])for(const colorScheme of ["light","dark"]){
    const name=width+"-"+colorScheme,context=await browser.newContext({viewport:{width,height:900},colorScheme,locale:"zh-TW",timezoneId:"Asia/Taipei",hasTouch:width===390,isMobile:width===390}),page=await context.newPage();
    let sourceOnline=true,sourceError="來源回應逾時",runnerOnline=true,failHistory=false,persisted=true,incidents=[],availability=[],connectionReads=0;
    const task={id:"windows-running",source_id:"windows",source_label:"Windows",runner:"local",project:"desktop",title:"Windows 正在執行的工作",summary:"目前工作持續執行。",status:"active",state:"observing",runner_online:true,updated:now-10,created_at:now-7200,started_at:now-7000,processes:[{process_id:"active-command",status:"running",display_status:"running",observed_at:now-5}],liveness:{observed_at:now,runner_last_seen:now,conversation:{finished_at:now-10},work:{running_count:1}}};
    const errorsForPage=[];
    page.on("pageerror",error=>{errorsForPage.push(error.message);errors.push(error.message);});
    await page.clock.install({time:new Date(now*1000)});
    await page.route("http://connections.test/**",route=>{
      const request=route.request(),url=new URL(request.url());
      if(url.pathname==="/manage")return route.fulfill({contentType:"text/html; charset=utf-8",body:fs.readFileSync(path.join(root,"src/webcodex/management.html"))});
      if(url.pathname.endsWith("/session"))return route.fulfill({json:{authenticated:true,password_enabled:true}});
      if(url.pathname.endsWith("/sources"))return route.fulfill({json:{sources:[{id:"windows",label:"Windows",runner_labels:{wsl:"Windows · WSL"}}]}});
      if(url.pathname==="/api/manage/connections"){
        connectionReads++;
        return route.fulfill(failHistory?{status:503,json:{error:"連線紀錄暫時無法讀取"}}:{json:{observed_at:now,incidents,sources:availability,persisted}});
      }
      if(!sourceOnline)return route.fulfill({status:502,json:{error:sourceError}});
      if(url.pathname.endsWith("/refresh"))return route.fulfill({json:{updates:[],observed_at:now}});
      if(url.pathname.endsWith("/state"))return route.fulfill({json:{tasks:[{...task,runner_online:runnerOnline}],runners:[{id:"local",online:runnerOnline,last_seen:runnerOnline?now:now-52}],projects:[{runner:"local",project:"desktop",environment_label:"Windows"}],next_offset:null,observed_at:now}});
      if(url.pathname.endsWith("/tasks/"+task.id))return route.fulfill({json:{...task,runner_online:runnerOnline,observed_at:now,outputs:[],errors:[]}});
      return route.fulfill({status:404,json:{error:"not found"}});
    });
    const ready=()=>page.waitForFunction(()=>!document.getElementById("refresh").disabled),refresh=async()=>{await page.locator("#refresh").click();await ready();};
    const dimensions=()=>page.evaluate(()=>{const row=document.querySelector(".task"),style=getComputedStyle(row),control=document.getElementById("refresh");return{root:getComputedStyle(document.documentElement).fontSize,task:{height:row.getBoundingClientRect().height,fontSize:style.fontSize,padding:style.padding,lineHeight:style.lineHeight},control:{height:control.getBoundingClientRect().height,minHeight:getComputedStyle(control).minHeight},layout:Object.fromEntries([".app-header","#sources","#filters","#work"].map(selector=>{const {x,y,width,height}=document.querySelector(selector).getBoundingClientRect();return[selector,{x,y,width,height}];})),overflow:document.documentElement.scrollWidth>document.documentElement.clientWidth};});
    try{
      await page.goto("http://connections.test/manage");await page.locator(".task").waitFor();await ready();
      const initial=await dimensions();assert.equal(initial.root,"14.4px");assert.equal(initial.task.padding,"10.8px 14.4px");assert.equal(initial.overflow,false);
      await page.screenshot({path:path.join(output,"connections-"+(baseline?"before":"after")+"-"+name+".png"),fullPage:width===390});
      if(baseline){measurements.connections.push({name,initial});continue;}
      assert.equal(initial.layout["#sources"].width,initial.layout["#filters"].width,"connection control preserves the full source row width");
      assert.ok(Math.abs(initial.layout["#filters"].y-initial.layout["#sources"].y-initial.layout["#sources"].height-10.8)<.05,"collapsed history preserves the source-to-filter gap");
      assert.equal(await page.locator(".app-header #connection-toggle").count(),1);
      assert.equal(connectionReads,0,"collapsed history does not fetch source diagnostics");await refresh();assert.equal(connectionReads,0);
      const toggle=page.locator("#connection-toggle");await toggle.focus();await page.keyboard.press("Enter");assert.equal(await toggle.getAttribute("aria-expanded"),"true");await page.getByText("沒有連線異常紀錄",{exact:true}).waitFor();
      incidents=[{id:"source-timeout",component:"source",source_id:"windows",runner_id:null,reason:"source_read_timeout",state:"active",started_at:now-45,last_seen_at:now, recovered_at:null,details:{operation:"state",timeout_seconds:10,url:"https://secret.example/?token=fixture-secret",error:"Bearer fixture-secret"}}];sourceOnline=false;
      await refresh();await page.locator("#connection-list .connection-reason").filter({hasText:"來源回應逾時"}).waitFor();assert.match(await page.locator("#connection-list").innerText(),/連線異常/);assert.doesNotMatch(await page.locator("#connection-list").innerText(),/fixture-secret|secret\.example/);
      incidents[0]={...incidents[0],state:"changed",closed_at:now-20,end_reason:"diagnostic_changed"};incidents.push({...incidents[0],id:"source-protocol",state:"active",reason:"source_invalid_response",started_at:now-20,closed_at:null,end_reason:null,details:{operation:"state"}});sourceError="來源回傳了無效資料";await refresh();
      const changedIncident=page.locator('[data-incident="source-timeout"]'),protocolIncident=page.locator('[data-incident="source-protocol"]');assert.equal(await changedIncident.locator(".status").textContent(),"原因已變更");assert.equal(await changedIncident.locator(".status").evaluate(el=>el.classList.contains("active")),false);assert.match(await changedIncident.innerText(),/來源回應逾時/);assert.doesNotMatch(await changedIncident.innerText(),/已復線|恢復/);assert.equal(await changedIncident.locator("dt").last().textContent(),"更新");assert.equal(await changedIncident.locator("time").last().getAttribute("datetime"),new Date((now-20)*1000).toISOString());assert.match(await protocolIncident.innerText(),/來源回應格式錯誤/);assert.equal(await protocolIncident.locator(".status").textContent(),"連線異常");await page.screenshot({path:path.join(output,"connections-reason-changed-"+name+".png"),fullPage:width===390});
      sourceOnline=true;incidents[1]={...incidents[1],state:"recovered",recovered_at:now};
      for(const operation of ["tasks","task-date","refresh","status"])incidents.push({...incidents[0],id:"operation-"+operation,state:"active",recovered_at:null,closed_at:null,end_reason:null,details:{operation}});
      await refresh();assert.match(await page.locator("#sources").innerText(),/Windows · 已連線/);assert.doesNotMatch(await page.locator("#connection-list").innerText(),/目前失聯/);
      for(const operation of ["任務清單","任務詳情","任務日期","程序觀察","更新狀態"])assert.match(await page.locator("#connection-list").innerText(),new RegExp(operation+" · 來源回應逾時"));
      runnerOnline=false;incidents.push({id:"runner-missed",component:"runner",source_id:"windows",runner_id:"local",reason:"runner_heartbeat_expired",state:"active",started_at:now-30,last_seen_at:now,recovered_at:null,details:{age_seconds:52,threshold_seconds:15}});
      await refresh();assert.match(await page.locator("#connection-list").innerText(),/Runner local/);assert.match(await page.locator("#connection-list").innerText(),/52 秒/);
      runnerOnline=true;incidents=incidents.map(incident=>incident.state==="changed"?incident:{...incident,state:"recovered",recovered_at:now,recovery_details:{age_seconds:0}});incidents.unshift({id:"server-stall",component:"server",source_id:"windows",runner_id:null,reason:"event_loop_blocked",state:"recovered",started_at:now-55,last_seen_at:now-37,recovered_at:now-37,details:{duration_ms:2100,stack:"Authorization: Bearer fixture-secret"},recovery_details:{duration_ms:18400}});
      await refresh();await toggle.focus();
      const response=page.waitForResponse(r=>new URL(r.url()).pathname==="/api/manage/connections");await page.clock.fastForward(10000);await (await response).finished();await ready();assert.equal(await toggle.evaluate(el=>el===document.activeElement),true,"automatic refresh keeps history control focused");
      const history=await page.locator("#connection-list").innerText();assert.match(history,/伺服器處理停頓/);assert.match(history,/18\.4 秒/);assert.match(history,/52 秒未收到心跳/);assert.match(history,/已復線/);assert.match(history,/恢復/);assert.doesNotMatch(history,/fixture-secret|Authorization/);assert.equal(await page.locator("#connection-list li").count(),8);assert.equal(await toggle.evaluate(el=>el.classList.contains("offline")),false);
      await page.screenshot({path:path.join(output,"connections-history-"+name+".png"),fullPage:width===390});
      const serverIncident=incidents[0];incidents[0]={...serverIncident,reason:"runner_status_unavailable",state:"active",recovered_at:null,details:{exception_type:"OperationalError"},recovery_details:{}};await refresh();
      const monitorIncident=page.locator('[data-incident="server-stall"]');assert.match(await monitorIncident.innerText(),/讀取執行環境狀態失敗/);assert.match(await monitorIncident.innerText(),/狀態待確認/);assert.doesNotMatch(await monitorIncident.innerText(),/處理停頓|目前失聯/);
      incidents[0]={...incidents[0],reason:"monitor_scheduling_gap",details:{gap_seconds:1847.5,observed_at:now}};await refresh();assert.match(await monitorIncident.innerText(),/監測排程中斷（1,847\.5 秒）/);assert.match(await monitorIncident.innerText(),/狀態待確認/);assert.doesNotMatch(await monitorIncident.innerText(),/處理停頓|目前失聯|已復線/);await page.screenshot({path:path.join(output,"connections-monitor-gap-"+name+".png"),fullPage:width===390});incidents[0]=serverIncident;
      failHistory=true;await refresh();assert.equal(await page.locator("#connection-list li").count(),8);await page.locator("#connection-error").filter({hasText:"連線紀錄讀取失敗"}).waitFor();assert.equal(await page.locator(".task").count(),1);failHistory=false;
      availability=[{source_id:"windows",source_label:"Windows",status:"unavailable"},{source_id:"older-source",source_label:"舊版來源",status:"unsupported"}];await refresh();assert.match(await page.locator("#connection-availability").innerText(),/Windows · 連線紀錄暫時無法讀取/);assert.match(await page.locator("#connection-availability").innerText(),/舊版來源 · 尚未提供連線紀錄/);
      persisted=false;availability=[{source_id:"windows",source_label:"Windows",status:"available",persisted:false}];await refresh();assert.match(await page.locator("#connection-availability").innerText(),/Windows · 連線紀錄尚未寫入磁碟/);assert.match(await page.locator("#connection-availability").innerText(),/本機 · 連線紀錄尚未寫入磁碟/);availability=[];persisted=true;
      const readsBeforeReload=connectionReads;await page.reload();await page.locator(".task").waitFor();await ready();assert.equal(connectionReads,readsBeforeReload);await toggle.click();await page.locator("#connection-list li").first().waitFor();assert.equal(await page.locator("#connection-list li").count(),8);assert.match(await page.locator("#connection-list").innerText(),/來源回應逾時/);assert.equal(await changedIncident.locator(".status").textContent(),"原因已變更");assert.doesNotMatch(await changedIncident.innerText(),/已復線|恢復/);
      await toggle.focus();await page.keyboard.press("Space");assert.equal(await toggle.getAttribute("aria-expanded"),"false");await page.keyboard.press("Space");assert.equal(await toggle.getAttribute("aria-expanded"),"true");
      const grown=await page.locator("#connection-toggle").evaluate(el=>el.getBoundingClientRect().height);if(width===390)assert.ok(grown>=44);
      await toggle.focus();await page.keyboard.press("Tab");assert.equal(await page.locator("#refresh").evaluate(el=>el===document.activeElement),true);await page.keyboard.press("Tab");assert.equal(await page.locator("#logout").evaluate(el=>el===document.activeElement),true);await page.keyboard.press("Tab");assert.equal(await page.locator("#connection-log").evaluate(el=>el===document.activeElement),true);
      await page.evaluate(()=>document.documentElement.style.fontSize="180%");assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>document.documentElement.clientWidth),false);assert.ok(await page.locator("#connection-log").evaluate(el=>el.scrollHeight>el.clientHeight));await page.keyboard.press("End");await page.waitForFunction(()=>{const el=document.getElementById("connection-log");return el.scrollTop>=el.scrollHeight-el.clientHeight-1;});await page.screenshot({path:path.join(output,"connections-text-200-"+name+".png"),fullPage:true});
      await page.emulateMedia({forcedColors:"active"});assert.equal(await page.locator("#connection-list .status").first().evaluate(el=>getComputedStyle(el).borderTopStyle),"solid");
      assert.deepEqual(errorsForPage,[]);measurements.connections.push({name,initial,connectionReads});checks.push(name+" source reason changes retain prior causes and update times without claiming recovery; monitor scheduling gaps show their duration; connection history, keyboard, touch, 200% text, forced colors and secret-safe output verified");
    }finally{await context.close();}
  }
}
module.exports=verifyConnections;
if(require.main===module)(async()=>{
  const {chromium}=require("playwright"),root=path.resolve(__dirname,".."),output=process.env.WEBCODEX_UI_EVIDENCE || fs.mkdtempSync(path.join(os.tmpdir(),"webcodex-connections-")),checks=[],errors=[],measurements={};
  fs.mkdirSync(output,{recursive:true});const browser=await chromium.launch({headless:true,...(process.env.WEBCODEX_CHROMIUM?{executablePath:process.env.WEBCODEX_CHROMIUM}:{}),args:["--no-sandbox"]});
  try{await verifyConnections(browser,root,output,checks,errors,measurements,{baseline:process.argv.includes("--baseline")});assert.deepEqual(errors,[]);fs.writeFileSync(path.join(output,process.argv.includes("--baseline")?"connections-baseline.json":"connections-results.json"),JSON.stringify({checks,measurements},null,2));console.log(JSON.stringify({output,checks,measurements}));}finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
