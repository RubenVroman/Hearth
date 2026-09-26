const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const ui = path.resolve(__dirname, '../../hearth/ui/static');

function eventTarget() {
  return {
    handlers: {},
    addEventListener(name, fn) { this.handlers[name] = fn; },
    removeEventListener(name, fn) { if (this.handlers[name] === fn) delete this.handlers[name]; },
  };
}

function element() {
  const classes = new Set();
  return {
    hidden: true, innerHTML: '', textContent: '', dataset: {}, scrollTop: 0, scrollHeight: 1800, clientHeight: 600,
    ...eventTarget(), setAttribute() {}, removeAttribute() {}, querySelectorAll() { return []; },
    classList: { add(...values) { values.forEach(x => classes.add(x)); }, remove(...values) { values.forEach(x => classes.delete(x)); }, contains(x) { return classes.has(x); }, toggle(x, value) { value ? classes.add(x) : classes.delete(x); } },
    scrollTo(options) { this.scrollTop = options.top; this.lastScroll = options; },
  };
}
function load() {
  let timerId=0;
  const timers=new Map();
  const elements = new Map();
  const getElementById = (id) => { if (!elements.has(id)) elements.set(id, element()); return elements.get(id); };
  const document={...eventTarget(),getElementById,querySelector(selector){return getElementById(selector);},querySelectorAll(){return [];},body:element(),documentElement:{dataset:{}},hidden:false};
  const context = {console, URL, URLSearchParams, document, navigator:{}, Element: class {}, localStorage:{getItem(){return null;}},
    setTimeout(fn){timers.set(++timerId,fn);return timerId;},clearTimeout(id){timers.delete(id);},setInterval(){return 0;},clearInterval(){},
    ...eventTarget(),matchMedia(){return {matches:false};},module:{exports:{}}};
  context.window=context;
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(ui,'presentation.js'),'utf8'),context);
  vm.runInContext(fs.readFileSync(path.join(ui,'voice-session.js'),'utf8'),context);
  vm.runInContext(fs.readFileSync(path.join(ui,'app.js'),'utf8').replace(/\nboot\(\);\s*$/, '\n'),context);
  const run = (source) => vm.runInContext(source,context);
  return {context,document,getElementById,run,timers,tick(){const [id,fn]=timers.entries().next().value;timers.delete(id);fn();}};
}

test('every result has visible complete metadata without selecting a card',()=>{
  const {run}=load();
  const html=run(`mediaMarkup({id:'media',kind:'media',data:{items:[
    {id:'1',title:'One',summary:'Full first synopsis',reason:'First reason',year:2020},
    {id:'2',title:'Two',summary:'Full second synopsis',reason:'Second reason',type:'show',rating:8.1}
  ]}})`);
  for(const value of ['One','Two','Full first synopsis','Full second synopsis','First reason','Second reason','2020','Series','8.1']) assert.ok(html.includes(value),value);
  assert.ok(html.includes('info-media-board'));
  assert.ok(!/<article[^>]*aria-hidden="true"/.test(html));
  assert.ok(!html.includes('is-recessed'));
  assert.ok(!html.includes('Swipe'));
});

test('unresolved and catalog results never pretend to be available or still searching',()=>{
  const {run}=load();
  const catalog=run(`mediaCardMarkup({id:'1',title:'One',source:'overseerr'})`);
  assert.ok(catalog.includes('Catalog match'));
  assert.ok(!catalog.includes('In your library'));
  const unresolved=run(`mediaCardMarkup({id:'2',title:'Unknown',skeleton:true,status:'unresolved',reason:'Metadata unavailable'})`);
  assert.ok(unresolved.includes('Metadata unavailable'));
  assert.ok(!unresolved.includes('Looking this up'));
  assert.ok(!unresolved.includes('Finding the details'));
});

test('untrusted result content and links cannot introduce markup or script URLs',()=>{
  const {run}=load();
  const html=run(`mediaCardMarkup({title:'<img src=x onerror=alert(1)>',summary:'<script>bad()</script>',links:{imdb:'javascript:alert(1)',tmdb:'https://www.themoviedb.org/movie/1'}})`);
  assert.ok(html.includes('&lt;script&gt;'));
  assert.ok(!html.includes('href="javascript:'));
  assert.ok(html.includes('https://www.themoviedb.org/movie/1'));
  const info=run(`HearthPresentation.informationMarkup({kind:'information',title:'Found',data:{items:[{title:'<b>Title</b>',body:'Details',url:'javascript:bad()'}],sources:[{url:'https://example.com/reference',title:'Source'}]}})`);
  assert.ok(info.includes('&lt;b&gt;Title'));
  assert.ok(info.includes('https://example.com/reference'));
  assert.ok(!info.includes('javascript:'));
  assert.equal(run(`HearthPresentation.safeUrl('https://secret:password@example.com')`),'');
});

test('bounded result lists explicitly show the total instead of implying completeness',()=>{
  const {run}=load();
  assert.ok(run(`mediaMarkup({kind:'media',data:{items:[{id:'1',title:'One'},{id:'2',title:'Two'}],total:14,truncated:true}})`).includes('2 of 14 titles'));
});

test('playback cards distinguish confirmed playback from ready, pending, and unknown states',()=>{
  const {run}=load();
  for (const [state, pending, expected] of [
    ['ready', false, 'Ready to play'], ['playing', true, 'Ready to play'],
    ['paused', false, 'Paused'], ['opening', false, 'Opening'],
    [null, false, 'Playback not confirmed'], ['playing', false, 'Now playing'],
  ]) {
    const html=run(`mediaCardMarkup(${JSON.stringify({title:'Arrival',player:'Infuse',state,pending})})`);
    assert.ok(html.includes('>' + expected + '</p>'), expected);
    if (expected !== 'Now playing') assert.ok(!html.includes('Now playing'));
  }
});

test('sideband end-call waits for final audio; partial or invalid arguments never hang up',()=>{
  const {run}=load();
  run(`var stopped=0;stopConversation=()=>{stopped+=1;state.call=null;};refresh=()=>{};
    state.call={sidebandOk:true,audioPlaying:true,responseGeneration:0,toolCalls:new Set()};
    var end={type:'function_call',call_id:'end',name:'end_call',arguments:'{}'};`);
  run(`onRealtimeEvent({...end,type:'response.function_call_arguments.done'})`);
  assert.equal(run('stopped'),0);
  run(`onRealtimeEvent({type:'response.done',response:{status:'cancelled',output:[end]}})`);
  assert.equal(run('stopped'),0);
  run(`onRealtimeEvent({type:'response.done',response:{status:'completed',output:[{...end,arguments:'bad'}]}})`);
  assert.equal(run('stopped'),0);
  run(`onRealtimeEvent({type:'response.done',response:{status:'completed',output:[end]}})`);
  assert.equal(run('state.call.pendingHangup'),true);
  assert.equal(run('stopped'),0);
  run(`onRealtimeEvent({type:'output_audio_buffer.stopped'})`);
  assert.equal(run('stopped'),1);
});

test('malformed final tool arguments do not invoke an action',async()=>{
  const {run}=load();
  run(`var invoked=0;var sent=[];state.call={callId:'call'};api=async()=>{invoked++;};sendRealtime=(e)=>sent.push(e);`);
  await run(`relayTool({name:'do_something',call_id:'tool',arguments:'['})`);
  assert.equal(run('invoked'),0);
  assert.equal(run(`JSON.parse(sent[0].item.output).ok`),false);
});

test('tool authorization text belongs to the current utterance, never a previous request',()=>{
  const {run}=load();
  run(`state.call={said:'old request',responseGeneration:0,inputItemId:'old'};appendLog=()=>{};noteOverlayConversation=()=>{};`);
  run(`onRealtimeEvent({type:'input_audio_buffer.speech_started',item_id:'new'})`);
  assert.equal(run('state.call.said'),'');
  run(`onRealtimeEvent({type:'conversation.item.input_audio_transcription.completed',item_id:'old',transcript:'stale request'})`);
  assert.equal(run('state.call.said'),'');
  run(`onRealtimeEvent({type:'conversation.item.input_audio_transcription.completed',item_id:'new',transcript:'current request'})`);
  assert.equal(run('state.call.said'),'current request');
  run(`onRealtimeEvent({type:'input_audio_buffer.speech_started',item_id:'third'});
    onRealtimeEvent({type:'conversation.item.input_audio_transcription.delta',item_id:'third',delta:'download '});`);
  assert.equal(run('state.call.said'),'');
  run(`onRealtimeEvent({type:'conversation.item.input_audio_transcription.completed',item_id:'third',transcript:''})`);
  assert.equal(run('state.call.said'),'');
});

test('a deliberate end-call cannot be mistaken for a reconnectable transport failure',()=>{
  const {run}=load();
  run(`var retries=0;recoverConversation=()=>{retries++;};state.call={pendingHangup:true};
    applyTransportDecision({action:'reconnect',reason:'peer_failed'});`);
  assert.equal(run('retries'),0);
});

test('new call instances keep lifecycle recovery and scoped tool state together',async()=>{
  const {run}=load();
  run(`var peers=[];var hangups=[];var received=0;
    var track={kind:'audio',enabled:true,addEventListener(){}};
    acquireMicStream=async()=>({getAudioTracks:()=>[track]});
    hideMicPanels=()=>{};showListeningChrome=()=>{};setRefreshInterval=()=>{};applyTransportDecision=()=>{};
    request=async()=>({ok:true,headers:{get:(name)=>name==='X-Hearth-Call-Id'?'real-session':name==='X-Hearth-Realtime-Path'?'webrtc-ga':''},text:async()=> 'answer'});
    RTCPeerConnection=class {
      constructor(){this.connectionState='connected';this.iceConnectionState='connected';peers.push(this);}
      addTrack(){} addEventListener(){} getSenders(){return [];} close(){}
      createDataChannel(){this.dc={readyState:'open',handlers:{},addEventListener(name,fn){this.handlers[name]=fn;}};return this.dc;}
      async createOffer(){return {sdp:'offer'};} async setLocalDescription(){} async setRemoteDescription(){}
    };`);
  await run('startConversation()');
  assert.equal(run('voiceLife.phase'),'live');
  assert.equal(run('state.call.sessionId'),'real-session');
  assert.equal(run('state.call.said'),'');
  assert.equal(run('state.call.toolCalls.size'),0);
  run(`onRealtimeEvent=()=>{received++;};voiceLife.beginUserStop();peers[0].dc.handlers.message({data:'{"type":"response.done"}'});`);
  assert.equal(run('received'),0);
});

test('speech focus and status polling preserve card DOM; enrichment updates it',()=>{
  const {run}=load();
  run(`var widget={id:'media',kind:'media',title:'One',updated_at:'first',data:{items:[{id:'1',title:'One'},{id:'2',title:'Two'}],active_id:'1'}}; var original=overlaySignature(widget);`);
  assert.equal(run(`widget.updated_at='later';widget.title='Two';widget.data.active_id='2';overlaySignature(widget)===original`),true);
  assert.equal(run(`widget.data.items[1].summary='New details';overlaySignature(widget)===original`),false);
});

test('hands-free reader reaches every page, pauses and resumes, and releases timers',()=>{
  const {context,document,timers,tick}=load();
  const viewport=element();
  const reader=new context.HearthPresentation.AmbientReader({viewport,button:element(),status:element(),document});
  reader.show('list');
  tick();assert.equal(viewport.scrollTop,432);
  tick();assert.equal(viewport.scrollTop,864);
  tick();assert.equal(viewport.scrollTop,1200);
  tick();assert.equal(viewport.scrollTop,0);
  reader.setPaused(true);assert.equal(timers.size,0);
  reader.show('list');assert.equal(reader.paused,true);
  reader.setPaused(false);assert.equal(timers.size,1);
  reader.show('new-list');assert.equal(viewport.scrollTop,0);
  reader.stop();assert.equal(timers.size,0);
});

test('result polling and spoken focus preserve mounted cards, reading position, and entrance timing',()=>{
  const {run,getElementById,timers}=load();
  const content=getElementById('info-content');
  let html='',writes=0;
  Object.defineProperty(content,'innerHTML',{get(){return html;},set(value){html=value;writes++;}});
  run(`var widget={id:'media',kind:'media',title:'One',data:{items:[{id:'1',title:'One'},{id:'2',title:'Two'}],active_id:'1'}};
    openInfoOverlay(widget);`);
  const entrance=run('state.infoEnterTimer');
  assert.equal(content.classList.contains('is-entering'),true);
  getElementById('info-glass-inner').scrollTop=240;
  run(`widget.updated_at='later';widget.title='Two';widget.data.active_id='2';openInfoOverlay(widget);`);
  assert.equal(writes,1);
  assert.equal(run('state.infoEnterTimer'),entrance);
  assert.equal(getElementById('info-glass-inner').scrollTop,240);
  const finish=timers.get(entrance);timers.delete(entrance);finish();
  run(`widget.data.items[1].summary='New details';openInfoOverlay(widget);`);
  assert.equal(writes,2);
  assert.equal(content.classList.contains('is-entering'),false,'metadata enrichment does not animate every card again');
  run(`widget.data.items=[{id:'3',title:'A new result'}];openInfoOverlay(widget);`);
  assert.equal(content.classList.contains('is-entering'),true);
  assert.equal(getElementById('info-glass-inner').scrollTop,0);
});

test('temporarily hidden results retain their DOM and keep home controls out of keyboard focus only while visible',()=>{
  const {run,document,getElementById}=load();
  run(`var widget={id:'media',kind:'media',data:{items:[{id:'1',title:'One'}]}};openInfoOverlay(widget);`);
  const stage=document.querySelector('.stage');
  const overlay=getElementById('info-overlay');
  const content=getElementById('info-content');
  const html=content.innerHTML;
  Object.defineProperty(content,'innerHTML',{get(){return html;},set(){throw new Error('unchanged board remounted');}});
  getElementById('info-glass-inner').scrollTop=120;
  assert.equal(stage.inert,true);
  assert.equal(overlay.inert,false);
  run('softHideInfoOverlay()');
  assert.equal(stage.inert,false);
  assert.equal(overlay.inert,true,'soft-hidden controls leave the tab sequence immediately');
  run('openInfoOverlay(widget)');
  assert.equal(stage.inert,true);
  assert.equal(overlay.inert,false);
  assert.equal(getElementById('info-glass-inner').scrollTop,120);
  run('closeInfoOverlay()');
  assert.equal(stage.inert,false);
  assert.equal(overlay.inert,true,'closing controls leave the tab sequence before the fade finishes');
});

test('focusing or touching results pauses automatic reading while the toolbar toggle can resume it',()=>{
  const {context,document,timers}=load();
  const viewport=element(),button=element();
  const reader=new context.HearthPresentation.AmbientReader({viewport,button,status:element(),document});
  reader.show('results');
  viewport.handlers.focusin({target:viewport});
  assert.equal(reader.paused,true,'tabbing into the scroll viewport gives the user reading control');
  assert.equal(timers.size,0);
  button.handlers.click();
  assert.equal(reader.paused,false);
  assert.equal(timers.size,1);
  viewport.handlers.pointerdown({target:viewport});
  assert.equal(reader.paused,true);
  assert.equal(timers.size,0);
  reader.destroy();
});

test('result entrances honor both system reduced motion and the Still look preference',()=>{
  for(const preference of ['system','look']) {
    const {run,context,document,getElementById}=load();
    if(preference==='system') context.matchMedia=()=>({matches:true});
    else document.documentElement.dataset.motion='still';
    run(`openInfoOverlay({id:'media',kind:'media',data:{items:[{id:'1',title:'One'}]}})`);
    assert.equal(getElementById('info-content').classList.contains('is-entering'),false,preference);
    assert.equal(run('state.infoEnterTimer'),null,preference);
    run('closeInfoOverlay()');
    assert.equal(getElementById('info-overlay').hidden,true,preference);
  }
});

test('reader follows keyboard and content resizing, then releases every observer when hidden',()=>{
  const {context,document,timers}=load();
  const observers=[];
  context.ResizeObserver=class {
    constructor(callback){this.callback=callback;this.observed=[];this.disconnected=false;observers.push(this);}
    observe(target){this.observed.push(target);}
    disconnect(){this.disconnected=true;}
  };
  context.visualViewport=eventTarget();
  const viewport=element(),content=element(),button=element();
  viewport.scrollHeight=500;
  const reader=new context.HearthPresentation.AmbientReader({viewport,content,button,status:element(),document});
  reader.show('list');
  assert.equal(button.hidden,true);
  assert.equal(timers.size,0,'short boards do not run pointless scrolling timers');
  assert.deepEqual(observers[0].observed,[viewport,content]);
  viewport.clientHeight=200;
  context.visualViewport.handlers.resize();
  assert.equal(button.hidden,false);
  assert.equal(timers.size,1,'keyboard shrink starts automatic reading when results no longer fit');
  reader.setPaused(true);
  viewport.clientHeight=300;
  observers[0].callback();
  assert.equal(timers.size,0,'geometry changes respect an explicit reading pause');
  reader.setPaused(false);
  viewport.scrollTop=200;
  viewport.scrollHeight=310;
  observers[0].callback();
  assert.equal(viewport.scrollTop,10,'shorter content clamps the reader to an existing page');
  reader.stop();
  assert.equal(observers[0].disconnected,true);
  assert.equal(context.handlers.resize,undefined);
  assert.equal(context.visualViewport.handlers.resize,undefined);
  assert.equal(document.handlers.visibilitychange,undefined);
  observers[0].callback();
  assert.equal(timers.size,0,'a late resize cannot revive a closed result board');
  reader.destroy();
  assert.deepEqual(Object.keys(viewport.handlers),[]);
  assert.deepEqual(Object.keys(button.handlers),[]);
});

test('fallback tools wait for a completed response, run once, and continue once per batch',async()=>{
  const {context,run}=load();
  run(`var sent=[];var invoked=[];refresh=()=>{};applyWidgetPayload=()=>{};
    state.call={callId:'session-one',sidebandOk:false,responseGeneration:0,toolCalls:new Set()};
    sendRealtime=(event)=>{sent.push(event);return true;};api=async(path,options)=>{invoked.push(JSON.parse(options.body));return {output:{ok:true}};};
    var result={response:{status:'completed',output:[{type:'function_call',call_id:'a',name:'search',arguments:'{}'},{type:'function_call',call_id:'b',name:'lookup',arguments:'{}'}]}};`);
  await run(`relayCompletedTools({...result,response:{...result.response,status:'cancelled'}})`);
  assert.equal(run('invoked.length'),0);
  await run('relayCompletedTools(result)');
  assert.equal(run('invoked.length'),2);
  assert.equal(run('invoked[0].session_id'),'session-one');
  assert.equal(run(`sent.filter(e=>e.type==='response.create').length`),1);
  await run('relayCompletedTools(result)');
  assert.equal(run('invoked.length'),2);
  assert.equal(run(`sent.filter(e=>e.type==='response.create').length`),1);
});

test('a new typed turn stops remaining tools in an earlier fallback batch',async()=>{
  const {run,getElementById}=load();
  run(`var invoked=[];var sent=[];var release;
    refresh=()=>{};applyWidgetPayload=()=>{};appendLog=()=>{};noteOverlayConversation=()=>{};flashLocalActivity=()=>{};
    state.call={callId:'session',said:'original request',sidebandOk:false,responseGeneration:0,toolCalls:new Set()};
    sendRealtime=(event)=>{sent.push(event);return true;};
    api=async(path,options)=>{invoked.push(JSON.parse(options.body));if(invoked.length>1)return {output:{ok:true}};return new Promise(resolve=>{release=()=>resolve({output:{ok:true}});});};
    var result={response:{status:'completed',output:[{type:'function_call',call_id:'a',name:'search',arguments:'{}'},{type:'function_call',call_id:'b',name:'act',arguments:'{}'}]}};`);
  const earlier=run('relayCompletedTools(result)');
  assert.equal(run('invoked.length'),1);
  getElementById('line').value='new request';
  await getElementById('composer').handlers.submit({preventDefault(){}});
  run('release()');
  await earlier;
  assert.equal(run('invoked.length'),1,'the old action must not use the newer request as authorization');
  assert.equal(run('invoked[0].said'),'original request');
  assert.equal(run('state.call.said'),'new request');
  assert.equal(run(`sent.filter(e=>e.type==='response.create').length`),1,'only the typed turn starts a new response');
});

test('a delayed completed response cannot authorize old tools with a newer typed turn',async()=>{
  const {run,getElementById}=load();
  run(`var invoked=[];refresh=()=>{};applyWidgetPayload=()=>{};appendLog=()=>{};noteOverlayConversation=()=>{};flashLocalActivity=()=>{};
    state.call={callId:'session',said:'old request',sidebandOk:false,responseGeneration:0,toolCalls:new Set()};
    sendRealtime=()=>true;api=async(path,options)=>{invoked.push(JSON.parse(options.body));return {output:{ok:true}};};
    onRealtimeEvent({type:'response.created',response:{id:'old'}});
    var result={response:{id:'old',status:'completed',output:[{type:'function_call',call_id:'a',name:'act',arguments:'{}'}]}};`);
  getElementById('line').value='new request';
  await getElementById('composer').handlers.submit({preventDefault(){}});
  await run('relayCompletedTools(result)');
  assert.equal(run('invoked.length'),0);
  run(`onRealtimeEvent({type:'response.created',response:{id:'new'}})`);
  await run('relayCompletedTools(result)');
  assert.equal(run('invoked.length'),0);
  await run(`relayCompletedTools({response:{...result.response,id:'new'}})`);
  assert.equal(run('invoked.length'),1);
  assert.equal(run('invoked[0].said'),'new request');
});
