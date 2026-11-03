"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const {chromium} = require("playwright");

(async () => {
  const root = path.resolve(__dirname,"..");
  const output = process.env.WEBCODEX_UI_EVIDENCE || path.join(root,".state/task-card-browser");
  fs.mkdirSync(output,{recursive:true});
  const origin = "http://task-card.test";
  let browser;
  try {
    browser = await chromium.launch({headless:true,
      ...(process.env.WEBCODEX_CHROMIUM ? {executablePath:process.env.WEBCODEX_CHROMIUM}:{}),
      args:["--no-sandbox"]});
    const page = await browser.newPage({viewport:{width:1280,height:1000}});
    await page.route(origin+"/**",route=>{
      const url = new URL(route.request().url());
      const file = url.pathname === "/task_app.html" ? path.join(root,"src/webcodex/task_app.html")
        : url.pathname === "/task_app_host.html" ? path.join(__dirname,"task_app_host.html") : null;
      return route.fulfill({status:file?200:404,contentType:"text/html; charset=utf-8",body:file?fs.readFileSync(file):""});
    });
    const errors=[],consoleMessages=[];page.on("pageerror",error=>errors.push(error.message));
    page.on("console",message=>{if(["error","warning"].includes(message.type()))consoleMessages.push(message.text());});
    const card=()=>page.frameLocator("#app");
    const open=async(query="")=>{await page.goto(origin+"/task_app_host.html"+query);await card().locator("#resume:enabled").waitFor();};
    const refresh=async()=>{
      const before=await page.evaluate(()=>window.taskCardTest.host.stateCalls);
      await card().locator("#check-status").click();
      await page.waitForFunction(previous=>window.taskCardTest.host.stateCalls>previous,before);
      await card().locator("#check-status:enabled").waitFor();
    };
    await open();
    await page.waitForTimeout(300);
    assert.equal(await page.evaluate(()=>window.taskCardTest.host.messages),0);
    assert.equal(await card().locator("#auto-message").isChecked(),false);
    await card().locator("#resume").dblclick();
    await page.waitForFunction(()=>window.taskCardTest.host.messages===1);
    await page.waitForTimeout(200);
    assert.equal(await page.evaluate(()=>window.taskCardTest.host.messages),1);
    assert.equal(await card().locator("#resume").isDisabled(),true);

    await open();
    await card().locator("#auto-message").click();
    await page.waitForFunction(()=>window.taskCardTest.host.autoEnabled===true);
    await page.waitForFunction(()=>window.taskCardTest.host.messages===1);
    assert.equal(await card().locator("#auto-count").innerText(),"1 / 3");
    await card().locator("#message-settings").evaluate(el=>el.open=true);
    await card().locator("#delivery-history").evaluate(el=>el.open=true);
    await page.locator("#app").evaluate(frame=>{frame.style.height=frame.contentDocument.documentElement.scrollHeight+"px";});
    await card().locator("main").screenshot({path:path.join(output,"task-card-desktop.png")});

    await open();
    await page.evaluate(()=>{window.taskCardTest.host.bound=false;});
    await card().locator("#check-status").click();
    await card().locator("#takeover:visible").waitFor();
    assert.match(await card().locator("#controller-state").innerText(),/其他視窗持有控制權/);
    assert.equal(await card().locator("#resume").isDisabled(),true);
    assert.equal(await card().locator("#auto-message").isChecked(),false);
    await card().locator("#takeover").click();
    await card().locator("#resume:enabled").waitFor();
    assert.equal(await page.evaluate(()=>window.taskCardTest.host.messages),0);
    assert.equal(await card().locator("#auto-message").isChecked(),false);
    await card().locator("#resume").click();
    await page.waitForFunction(()=>window.taskCardTest.host.messages===1);

    await open();
    await page.locator("#fail-state").click();
    await card().locator("#check-status").click();
    await card().locator("#reconnect:visible").waitFor();
    assert.match(await card().locator("#work-observation").innerText(),/舊紀錄/);
    assert.match(await card().locator("#conversation-activity").innerText(),/舊紀錄/);
    assert.equal(await card().locator("#runner-activity").innerText(),"連線狀態待更新");
    assert.equal(await page.evaluate(()=>window.taskCardTest.host.messages),0);

    await open("?activity");
    assert.equal(page.url(),origin+"/task_app_host.html?activity");
    assert.equal(await page.title(),"Task App 診斷測試頁");
    assert.equal(await card().getByText("本對話工具呼叫",{exact:true}).isVisible(),true);
    await page.locator("#quiet-activity").click();
    await refresh();
    const conversationCall=await card().locator("#conversation-activity").innerText();
    assert.match(conversationCall,/5 分鐘前/);
    await page.locator("#task-activity").click();
    await refresh();
    assert.equal(await card().locator("#conversation-activity").innerText(),conversationCall);
    assert.equal(await card().locator("#work-observation").innerText(),"沒有執行中的背景工作");
    await page.evaluate(()=>{window.taskCardTest.host.activityOutcome="running";window.taskCardTest.host.activityFinishedAt=null;});
    await refresh();
    assert.match(await card().locator("#conversation-activity").innerText(),/工具等候回應/);
    await page.evaluate(()=>{window.taskCardTest.host.activityOutcome="returned";window.taskCardTest.host.activityFinishedAt=Date.now()/1000;});
    await refresh();
    assert.equal(await card().locator("#conversation-activity").innerText(),conversationCall);
    assert.equal(await page.evaluate(()=>window.taskCardTest.host.messages),0);
    await card().locator("main").screenshot({path:path.join(output,"conversation-call-desktop.png")});
    await page.evaluate(()=>{window.taskCardTest.host.activityStartedAt=null;});
    await refresh();
    assert.equal(await card().locator("#conversation-activity").innerText(),"尚無紀錄");
    await page.locator("#file-activity").click();
    await refresh();
    assert.match(await card().locator("#conversation-activity").innerText(),/修改檔案/);

    await page.setViewportSize({width:390,height:1000});await open();
    assert.equal(await card().locator("body").evaluate(el=>el.scrollWidth>document.documentElement.clientWidth),false);
    await card().locator("main").screenshot({path:path.join(output,"task-card-mobile.png")});
    assert.deepEqual(errors,[]);
    assert.deepEqual(consoleMessages,[]);
    const checks=["default zero messages","manual double click once","explicit auto and audit","controller loss","explicit takeover without send","offline history","conversation call ignores task updates","tool return preserves request start","missing conversation has no task fallback","new conversation call advances activity","390px layout"];
    const result={passed:checks.length,checks,pageErrors:errors,consoleMessages};
    fs.writeFileSync(path.join(output,"result.json"),JSON.stringify(result,null,2));
    console.log(JSON.stringify(result));
  } finally {
    if(browser)await browser.close();
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
