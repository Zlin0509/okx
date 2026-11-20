// Run with the Playwright browser_run_code tool against the static-only server:
// python3 -m http.server 8766 --bind 127.0.0.1 --directory okx_workbench
// All API routes below are mocked; no backend, credentials or exchange are used.
async (page, screenshotDir = null) => {
  const ensure = (condition, message) => { if (!condition) throw new Error(message); };
  const results = [];
  await page.unrouteAll({behavior:'wait'});
  await page.clock.install({time:new Date('2026-11-13T03:00:00Z')});
  const now = Date.parse('2026-11-13T03:00:00Z');
  const candles = Array.from({length:60},(_,i)=>({ts:now-(60-i)*3600000,open:60000+i,high:60100+i,low:59900+i,close:60005+i}));
  const risk = {max_order_usdt:'100',max_position_usdt:'500',max_equity_fraction:'0.1',slippage_bps:'20',max_signal_age_seconds:300};
  const report = {id:1,source:'offline',initial_capital:10000,return_pct:1.25,max_drawdown_pct:0.5,win_rate_pct:60,trade_count:5,fees:1,
    curve:[{ts:now-3600000,equity:10000},{ts:now,equity:10125}],assumptions:'Mock snapshot',
    risk:{initial_effective_budget_usdt:100,slippage_bps:20,execution_price_offset_bps:20},
    snapshot:{symbol:'BTC-USDT-SWAP',bar:'1H',fast:8,slow:24,allocation:0.05,initial_capital:10000,fee_bps:5,slippage_bps:20,source:'offline',mode:'demo',data_start_ts:now-3600000,data_end_ts:now,generated_at_ms:now}};
  const state = {mode:'demo',source:'offline',credentials_ready:false,live_supported:false,control_epoch:10,risk,
    strategies:[{id:1,spec:{symbol:'BTC-USDT-SWAP',bar:'1H',fast:8,slow:24,allocation:0.05},active:false,source:'offline',validation:report}],orders:[],blockers:[],events:[]};
  let failState=false, delayedStart=false, releaseStart, startSeen, ticks=0, reportCounter=0;
  const generatedRequests=[], marketBars=[];
  await page.route('**/api/**', async route=>{
    const path=new URL(route.request().url()).pathname;
    if(path==='/api/state') {if(failState) return route.abort('failed');return route.fulfill({json:structuredClone(state)});}
    if(path==='/api/account') return route.fulfill({json:{source:'offline',equity:10100,available:9000,unrealized_pnl:12,realized:108,fees:9.75}});
    if(path==='/api/market') {
      const bar=new URL(route.request().url()).searchParams.get('bar');
      marketBars.push(bar);
      return route.fulfill({json:{source:'offline',candles:bar==='1m'?candles.map((c,i)=>({...c,ts:now-(60-i)*60000})):candles}});
    }
    if(path==='/api/strategies/generate') {
      const request=route.request().postDataJSON();generatedRequests.push(request);
      const spec={...request,fast:2,slow:6};
      const validation={...report,snapshot:{...report.snapshot,...spec}};
      const generated={id:2,spec,validation,source:'offline',method:'Mock parameter selection'};
      state.strategies=[{...generated,active:false}];
      return route.fulfill({json:generated});
    }
    if(path.endsWith('/signal')) return route.fulfill({json:{age_seconds:60,max_age_seconds:300,executable:true}});
    if(path==='/api/backtests') return route.fulfill({json:{...report,id:++reportCounter,snapshot:{...report.snapshot,...route.request().postDataJSON()}}});
    if(path==='/api/stop') {state.strategies[0].active=false;state.control_epoch++;return route.fulfill({json:{stopped:true,control_epoch:state.control_epoch}});}
    if(path.endsWith('/start')) {
      startSeen?.();
      if(delayedStart) {await new Promise(resolve=>releaseStart=resolve);return route.fulfill({json:{active:1}});} // Simulate even a stale server success: UI must ignore it.
      state.strategies[0].active=true;state.control_epoch++;return route.fulfill({json:{active:1}});
    }
    if(path.endsWith('/tick')) {ticks++;return route.fulfill({json:{action:'expired',signal:{age_seconds:301,max_age_seconds:300}}});}
    if(path.endsWith('/export.csv')) return route.fulfill({body:'ts,equity,symbol,fast,slow\n0,10000,BTC-USDT-SWAP,8,24\n',contentType:'text/csv'});
    return route.fulfill({status:404,json:{detail:'Unexpected mock path: '+path}});
  });
  for (const width of [1440,390]) {
    await page.setViewportSize({width,height:width===390?844:1000});
    failState=false;delayedStart=false;state.strategies[0].active=false;state.orders=[];state.blockers=[];reportCounter=0;
    state.strategies=[{id:1,spec:{symbol:'BTC-USDT-SWAP',bar:'1H',fast:8,slow:24,allocation:0.05},active:false,source:'offline',validation:report}];
    await page.goto('http://127.0.0.1:8766/static/index.html');
    await page.waitForFunction(()=>document.querySelector('#connection-notice').textContent.includes('已连接'));
    await page.locator('#backtest').click();
    await page.waitForFunction(()=>document.querySelector('#backtest-snapshot').textContent.includes('BTC-USDT-SWAP'));
    await page.selectOption('#symbol','ETH-USDT-SWAP');
    await page.locator('#fast').fill('20');await page.locator('#slow').fill('80');
    ensure(await page.locator('#backtest-stale').isVisible(),'input change must mark stale result');
    ensure((await page.locator('#backtest-snapshot').innerText()).includes('MA 8/24'),'snapshot changed to new form values');
    ensure((await page.locator('#export').innerText()).includes('上次'),'CSV must identify old result');
    ensure((await page.locator('#export').getAttribute('href')).includes('/1/'),'CSV id changed with form');
    results.push({width,scenario:'snapshot_and_csv',passed:true});
    await page.locator('#backtest').click();
    await page.waitForFunction(()=>document.querySelector('#backtest-snapshot').textContent.includes('ETH-USDT-SWAP'));
    ensure((await page.locator('#backtest-snapshot').innerText()).includes('MA 20/80'),'rerun snapshot not updated');
    ensure(!await page.locator('#backtest-stale').isVisible(),'rerun left stale warning');
    ensure((await page.locator('#export').getAttribute('href')).includes('/2/'),'new CSV not linked to new result');

    ensure(await page.locator('#bar').inputValue()==='1H','default timeframe unexpectedly changed');
    const previousGenerations=generatedRequests.length, previousMarketRequests=marketBars.length, ticksBeforeGenerate=ticks;
    await Promise.all([
      page.waitForResponse(response=>new URL(response.url()).pathname==='/api/market' && new URL(response.url()).searchParams.get('bar')==='1m'),
      page.selectOption('#bar','1m')
    ]);
    await page.locator('#generate').click();
    await page.waitForFunction(()=>document.querySelector('#selected-strategy').textContent.includes('#2') && document.querySelector('#selected-strategy').textContent.includes('1m'));
    await page.waitForFunction(()=>document.querySelector('#backtest-snapshot').textContent.includes('/ 1m /'));
    ensure(generatedRequests.length===previousGenerations+1 && generatedRequests.at(-1).bar==='1m','minute generation request missing or wrong');
    ensure(marketBars.slice(previousMarketRequests).includes('1m'),'minute market request missing');
    ensure(await page.locator('#fast').inputValue()==='2' && await page.locator('#slow').inputValue()==='6','generated parameters not applied');
    ensure(!state.strategies[0].active && state.orders.length===0 && ticks===ticksBeforeGenerate,'minute generation unexpectedly started trading');
    ensure(await page.locator('#tick').isDisabled(),'new minute strategy can execute before activation');
    await page.locator('#backtest').click();
    await page.waitForFunction(()=>document.querySelector('#export').getAttribute('href')?.includes('/3/'));
    ensure((await page.locator('#backtest-snapshot').innerText()).includes('/ 1m /'),'minute backtest snapshot lost timeframe');
    await page.selectOption('#bar','1H');
    ensure(await page.locator('#backtest-stale').isVisible(),'timeframe change did not mark minute result stale');
    ensure((await page.locator('#backtest-snapshot').innerText()).includes('/ 1m /'),'form change overwrote minute snapshot');
    ensure((await page.locator('#export').getAttribute('href')).includes('/3/'),'form change replaced minute CSV');
    results.push({width,scenario:'minute_selection_generation_and_backtest',passed:true});

    delayedStart=true;let seen=new Promise(resolve=>startSeen=resolve);
    await page.locator('#start').click();await page.locator('#confirm-start').click();await seen;
    const previousTicks=ticks;
    await page.getByRole('button',{name:'返回检查'}).click();
    await page.waitForFunction(()=>document.querySelector('#execution-state').textContent==='已暂停');
    releaseStart();
    await page.waitForFunction(()=>!document.querySelector('#confirm-start').disabled);
    ensure(ticks===previousTicks,'late start response caused a tick after stop');
    ensure(!state.strategies[0].active,'mock strategy resumed after pause');
    results.push({width,scenario:'late_start_ignored_after_pause',passed:true});

    state.strategies[0].active=true;delayedStart=false;
    await page.locator('#refresh').click();
    await page.waitForFunction(()=>document.querySelector('#execution-state').textContent==='运行中');
    failState=true;
    await page.locator('#refresh').click();
    await page.waitForFunction(()=>document.querySelector('#execution-state').textContent==='状态待同步');
    await page.clock.fastForward(7000);
    ensure(await page.locator('#connection-notice').isVisible(),'running disconnect not persistently visible');
    ensure((await page.locator('#equity').innerText())==='—','running disconnect kept stale money');
    ensure(await page.locator('#execution-dot').evaluate(el=>el.classList.contains('inactive')),'disconnected dot still green');
    ensure(await page.locator('#start').isDisabled() && await page.locator('#tick').isDisabled(),'disconnected controls enabled');
    ensure(state.strategies[0].active,'UI fabricated a server pause on disconnect');
    results.push({width,scenario:'running_disconnected',passed:true});
    if(screenshotDir) await page.screenshot({path:screenshotDir+'/disconnected-'+width+'.png',fullPage:true});
    failState=false;
    await page.locator('#refresh').click();
    await page.waitForFunction(()=>document.querySelector('#execution-state').textContent==='运行中');
    failState=true;
    await page.locator('#stop').click();
    await page.waitForFunction(()=>document.querySelector('#connection-notice').textContent.includes('暂停已确认，数据刷新失败'));
    await page.clock.fastForward(7000);
    ensure(await page.locator('#connection-notice').isVisible(),'persistent connection notice hidden on mobile');
    ensure((await page.locator('#execution-state').innerText()).includes('待同步'),'stale execution state still shown');
    ensure((await page.locator('#equity').innerText())==='—','stale money still shown');
    ensure(await page.locator('#start').isDisabled() && await page.locator('#tick').isDisabled(),'unsafe controls left enabled');
    ensure(!state.strategies[0].active,'stop was not acknowledged by mock server');
    results.push({width,scenario:'stop_acknowledged_refresh_failed',passed:true});

    failState=false;
    const blocker={id:'unknown123456789',symbol:'BTC-USDT-SWAP',side:'buy',quantity:'0.01',price:'60000',state:'unknown',filled:'0',error:'查询不到订单，禁止重发',not_found_count:2,last_error_code:'51603',created:now/1000};
    state.orders=[blocker];state.blockers=[blocker];state.events=Array.from({length:4},(_,i)=>({created:now/1000,level:'info',message:'普通事件 '+i}));
    await page.locator('#refresh').click();
    await page.waitForFunction(()=>document.querySelector('#blocker-notice').textContent.includes('unknown123456789'));
    ensure(await page.locator('#start').isDisabled(),'blocked account can start strategy');
    ensure((await page.locator('#blocker-notice').innerText()).includes('禁止重发'),'recovery instruction absent');
    blocker.not_found_count=100;
    await page.locator('#refresh').click();
    await page.waitForFunction(()=>document.querySelector('#blocker-notice').textContent.includes('100 次'));
    ensure((await page.locator('#blocker-notice').innerText()).includes('禁止人工作废'),'unknown abandonment prohibition absent');
    ensure(await page.getByRole('button',{name:'人工审计解除'}).count()===0,'not-found count exposed unsafe release');
    ensure(await page.locator('#recovery-dialog').count()===0,'unsafe recovery form retained');
    await page.locator('#orders').scrollIntoViewIfNeeded();
    const box=await page.locator('.order-status').boundingBox();
    ensure(box.x>=0 && box.x+box.width<=width,'status requires horizontal scroll');
    results.push({width,scenario:'persistent_blocker_mobile_status',passed:true});
    ensure(!await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),'horizontal page overflow');
    await page.evaluate(()=>scrollTo(0,0));
    if(screenshotDir) {await page.locator('#toast').evaluate(el=>el.classList.add('hidden'));await page.screenshot({path:screenshotDir+'/blockers-'+width+'.png',fullPage:true});}
  }
  await page.goto('about:blank'); // Stop page timers before removing mocks.
  await page.unrouteAll({behavior:'wait'});
  return {all_apis_mocked:true,checks:results,tick_count:ticks};
}
