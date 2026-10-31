"use strict";
const assert=require("node:assert/strict");
const fs=require("node:fs");
const path=require("node:path");
const {chromium}=require("playwright");
(async()=>{
  const root=path.resolve(__dirname,"..");
  const output=process.env.WEBCODEX_UI_EVIDENCE || path.join(root,".state/management-browser");
  fs.mkdirSync(output,{recursive:true});
  const now=Date.now()/1000;
  const task={id:"a".repeat(32),title:"WebCodex 開發",runner:"ll1",project:"webcodex",status:"active",state:"observing",summary:"檢查目前的命令結果",runner_online:true,stale:false,updated:now,
    processes:[{process_id:"process-1",status:"running",exit_code:null,observed_at:now}],liveness:{conversation:{finished_at:now-5}}};
  const offline={...task,id:"b".repeat(32),title:"PinLens 桌面測試",runner:"windows",project:"pinlens",state:"waiting_runner",runner_online:false,stale:true};
  const older={...task,id:"c".repeat(32),title:"較早的長命令",processes:[{process_id:"old-process",status:"running",exit_code:null,observed_at:now-600}]};
  const allTasks=[task,offline,...Array.from({length:58},(_,i)=>({...task,id:"extra-"+i,title:"已完成 "+i,state:"completed",processes:[]})),older];
  let browser,holdState=false,stateHeld=false,releaseState=null,offlineApi=false;
  try{
    browser=await chromium.launch({headless:true,...(process.env.WEBCODEX_CHROMIUM?{executablePath:process.env.WEBCODEX_CHROMIUM}:{}),args:["--no-sandbox"]});
    const page=await browser.newPage({viewport:{width:1400,height:1000}}),errors=[],calls=[];
    page.on("pageerror",error=>errors.push(error.message));
    await page.route("http://management.test/**",async route=>{
      const request=route.request(),url=new URL(request.url());
      if(url.pathname==="/manage")return route.fulfill({contentType:"text/html; charset=utf-8",body:fs.readFileSync(path.join(root,"src/webcodex/management.html"))});
      calls.push({path:url.pathname,method:request.method(),auth:request.headers().authorization,body:request.postData()});
      if(offlineApi)return route.abort("connectionfailed");
      if(request.headers().authorization!=="Bearer fixture-token")return route.fulfill({status:401,json:{error:"unauthorized"}});
      if(url.pathname==="/api/manage/refresh"){
        const body=request.postDataJSON();
        if(body.task_id===older.id||body.task_ids?.includes(older.id)){
          older.processes[0].status="completed";older.processes[0].exit_code=0;older.state="ready";
        }
        return route.fulfill({json:{updates:[],observed_at:now}});
      }
      if(url.pathname==="/api/manage/state"){
        if(holdState){stateHeld=true;await new Promise(resolve=>{releaseState=resolve;});}
        const offset=Number(url.searchParams.get("offset")||0);
        return route.fulfill({json:{tasks:allTasks.slice(offset,offset+60),next_offset:offset+60<allTasks.length?offset+60:null,observed_at:now,runners:[{id:"ll1",online:true,last_seen:now},{id:"windows",online:false,last_seen:now-600}]}});
      }
      const selected=allTasks.find(t=>url.pathname.endsWith(t.id));
      if(!selected)return route.fulfill({status:404,json:{error:"not found"}});
      return route.fulfill({json:{...selected,observed_at:now,outputs:[{...selected.processes[0],output:"job started\nvalidation passed\n"}],errors:[]}});
    });
    await page.goto("http://management.test/manage");
    assert.equal(await page.locator("#auth").isVisible(),true);
    await page.locator("#token").fill("invalid");await page.locator("#login").click();
    await page.locator("#error").filter({hasText:"Token 無效"}).waitFor();
    assert.equal(await page.locator("#dashboard").isHidden(),true);
    await page.locator("#token").fill("fixture-token");await page.locator("#token").press("Enter");
    await page.locator("#tasks .task").first().waitFor();
    assert.equal(await page.locator("#token").inputValue(),"");
    assert.equal(await page.evaluate(()=>localStorage.length+sessionStorage.length),0);
    assert.equal(await page.locator(".runner").count(),2);
    await page.locator("#tasks .task").first().click();
    await page.locator("#detail pre").filter({hasText:"validation passed"}).waitFor();
    assert.equal(await page.locator("#detail").innerText().then(t=>t.includes("observing")),false);
    await page.screenshot({path:path.join(output,"management-desktop.png"),fullPage:true});
    await page.locator("#query").fill("PinLens");assert.equal(await page.locator("#tasks .task").count(),1);
    await page.locator("#tasks .task").click();await page.locator("#detail .badge").filter({hasText:"舊紀錄"}).waitFor();
    assert.match(await page.locator("#detail").innerText(),/舊紀錄/);
    await page.setViewportSize({width:390,height:1000});
    assert.equal(await page.locator("body").evaluate(el=>el.scrollWidth>document.documentElement.clientWidth),false);
    await page.screenshot({path:path.join(output,"management-mobile.png"),fullPage:true});
    offlineApi=true;await page.locator("#refresh").click();
    await page.locator("#detail .badge").filter({hasText:"管理頁連線中斷"}).waitFor();
    assert.match(await page.locator("#runners").innerText(),/連線狀態待更新/);
    assert.match(await page.locator("#tasks").innerText(),/舊紀錄/);
    offlineApi=false;await page.locator("#refresh").click();
    await page.waitForFunction(()=>document.getElementById("error").textContent==='');
    await page.locator("#query").fill("");
    await page.locator("#more").click();
    await page.locator("#tasks .task").filter({hasText:older.title}).waitFor();
    assert.equal(await page.locator("#tasks .task").count(),61);
    await page.locator("#tasks .task").filter({hasText:older.title}).click();
    await page.locator("#detail .badge").filter({hasText:"已完成"}).waitFor();
    await page.locator("#refresh").click();
    await page.waitForFunction(()=>!document.getElementById("refresh").disabled);
    assert.equal(await page.locator("#tasks .task").count(),61,"refresh keeps loaded pages");
    assert.equal(await page.locator("#tasks .selected strong").textContent(),older.title);
    assert.equal(await page.locator("#detail h2").textContent(),older.title);
    assert.ok(calls.some(x=>x.path==="/api/manage/refresh"&&JSON.parse(x.body).task_ids?.includes(older.id)),"refresh includes the selected task beyond the first page");
    assert.ok(calls.filter(x=>x.path==="/api/manage/refresh").every(x=>(JSON.parse(x.body).task_ids||[]).length<=100));
    holdState=true;await page.locator("#refresh").click();
    for(let i=0;i<50&&!stateHeld;i++)await page.waitForTimeout(10);
    assert.equal(stateHeld,true);await page.locator("#logout").click();releaseState();
    await page.waitForTimeout(100);
    assert.equal(await page.locator("#auth").isVisible(),true);
    assert.equal(await page.locator("#tasks .task").count(),0,"a late refresh must not restore task data after logout");
    assert.equal(await page.locator("#detail").innerText(),"");
    assert.deepEqual(errors,[]);
    assert.ok(calls.filter(x=>x.method==="POST").every(x=>x.path==="/api/manage/refresh"));
    const result={passed:11,checks:["invalid token","login and memory-only token","runner overview","task output","search and offline history","390px layout","logout fences late responses","read-only request scope","network history markers","bounded visible task refresh","pagination and selected task survive refresh"],pageErrors:errors};
    fs.writeFileSync(path.join(output,"result.json"),JSON.stringify(result,null,2));console.log(JSON.stringify(result));
  }finally{if(releaseState)releaseState();if(browser)await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
