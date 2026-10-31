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
    const errors=[];page.on("pageerror",error=>errors.push(error.message));
    const card=()=>page.frameLocator("#app");
    const open=async()=>{await page.goto(origin+"/task_app_host.html");await card().locator("#resume:enabled").waitFor();};
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

    await page.setViewportSize({width:390,height:1000});await open();
    assert.equal(await card().locator("body").evaluate(el=>el.scrollWidth>document.documentElement.clientWidth),false);
    await card().locator("main").screenshot({path:path.join(output,"task-card-mobile.png")});
    assert.deepEqual(errors,[]);
    const result={passed:7,checks:["default zero messages","manual double click once","explicit auto and audit","controller loss","explicit takeover without send","offline history","390px layout"],pageErrors:errors};
    fs.writeFileSync(path.join(output,"result.json"),JSON.stringify(result,null,2));
    console.log(JSON.stringify(result));
  } finally {
    if(browser)await browser.close();
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
