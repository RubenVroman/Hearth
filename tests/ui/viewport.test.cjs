const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.resolve(__dirname, '../../hearth/ui/static/pwa.js'), 'utf8');

function load({ visualViewport = true, secure = true } = {}) {
  const properties = new Map();
  const handlers = new Map();
  const viewportHandlers = new Map();
  const frames = new Map();
  const timers = new Map();
  const observed = [];
  const registrations = [];
  let nextId = 0;
  let writes = 0;
  let resizeCallback;
  const element = (height) => ({ height, getBoundingClientRect() { return { height: this.height }; } });
  const header = element(82);
  const controls = element(180);
  const composer = element(54);
  const document = {
    documentElement: {
      clientHeight: 800, dataset: {}, style: {
        getPropertyValue(name) { return properties.get(name); },
        setProperty(name, value) { properties.set(name, value); writes += 1; },
      },
    },
    activeElement: null,
    querySelector(selector) { return ({ '.top': header, '.interaction-dock': controls, '.composer-dock': composer })[selector]; },
  };
  const viewport = { height: 800, offsetTop: 0, scale: 1, addEventListener(name, fn) { viewportHandlers.set(name, fn); } };
  const context = {
    document, innerHeight: 800, innerWidth: 390,
    visualViewport: visualViewport ? viewport : undefined,
    screen: { orientation: { addEventListener(name, fn) { handlers.set(`orientation:${name}`, fn); } } },
    addEventListener(name, fn) { handlers.set(name, fn); },
    requestAnimationFrame(fn) { frames.set(++nextId, fn); return nextId; },
    setTimeout(fn) { timers.set(++nextId, fn); return nextId; },
    clearTimeout(id) { timers.delete(id); },
    ResizeObserver: class { constructor(fn) { resizeCallback = fn; } observe(node) { observed.push(node); } },
    navigator: { serviceWorker: { register(url) { registrations.push(url); return Promise.resolve(); } } },
    location: { protocol: secure ? 'https:' : 'http:', hostname: 'hearth.example' },
  };
  context.window = context;
  vm.runInNewContext(source, context);
  return {
    context, viewport, document, header, controls, composer, properties, frames, timers, observed, registrations,
    get writes() { return writes; },
    emit(name) { handlers.get(name)(); },
    viewportEvent(name) { viewportHandlers.get(name)(); },
    resize() { resizeCallback(); },
    frame() { const queue = [...frames.values()]; frames.clear(); queue.forEach(fn => fn()); },
    type() { document.activeElement = { tagName: 'INPUT', type: 'text' }; },
  };
}

test('measures the visible viewport and both chrome regions before the first resize', () => {
  const env = load();
  assert.equal(env.properties.get('--app-height'), '800px');
  assert.equal(env.properties.get('--viewport-top'), '0px');
  assert.equal(env.properties.get('--header-height'), '82px');
  assert.equal(env.properties.get('--controls-height'), '180px');
  assert.ok(env.observed.includes(env.header));
  assert.ok(env.observed.includes(env.controls));
  assert.deepEqual(env.registrations, ['/sw.js']);
});

test('iOS keyboard uses the visible height once and follows viewport panning', () => {
  const env = load();
  env.type();
  env.viewport.height = 470;
  env.viewport.offsetTop = 24;
  env.emit('focusin');
  env.viewportEvent('resize');
  env.viewportEvent('scroll');
  assert.equal(env.frames.size, 1);
  env.frame();
  assert.equal(env.properties.get('--app-height'), '470px');
  assert.equal(env.properties.get('--viewport-top'), '24px');
  assert.equal(env.properties.get('--keyboard-inset'), '306px');
  assert.equal(env.properties.get('--dock-safe-bottom'), '0px');
  assert.equal(env.document.documentElement.dataset.keyboard, 'true');
});

test('Android content resize detects the keyboard without shrinking the viewport twice', () => {
  const env = load();
  env.type();
  env.context.innerHeight = env.viewport.height = 440;
  env.emit('resize');
  env.frame();
  assert.equal(env.properties.get('--keyboard-inset'), '360px');
  assert.equal(env.properties.get('--app-height'), '440px');
  env.document.activeElement = null;
  env.context.innerHeight = env.viewport.height = 800;
  env.emit('focusout');
  env.frame();
  assert.equal(env.properties.get('--keyboard-inset'), '0px');
  assert.equal(env.properties.get('--dock-safe-bottom'), 'env(safe-area-inset-bottom, 0px)');
});

test('orientation repairs delayed viewport values and discards the portrait keyboard baseline', () => {
  const env = load();
  env.type();
  env.emit('orientationchange');
  env.emit('orientation:change');
  assert.equal(env.frames.size, 1);
  assert.equal(env.timers.size, 1);
  env.frame();
  env.context.innerWidth = 844;
  env.context.innerHeight = env.viewport.height = 390;
  env.frame();
  assert.equal(env.properties.get('--app-height'), '390px');
  assert.equal(env.properties.get('--keyboard-inset'), '0px');
  env.frame();
  assert.equal(env.frames.size, 0);
});

test('resize bursts and unchanged ResizeObserver notifications do not create layout loops', () => {
  const env = load();
  env.header.height = 101.2;
  env.controls.height = 207.4;
  for (let i = 0; i < 10; i++) {
    env.resize();
    env.emit('resize');
    env.viewportEvent('scroll');
  }
  assert.equal(env.frames.size, 1);
  env.frame();
  assert.equal(env.properties.get('--header-height'), '102px');
  assert.equal(env.properties.get('--controls-height'), '208px');
  const writes = env.writes;
  env.resize();
  env.frame();
  assert.equal(env.writes, writes);
  assert.equal(env.frames.size, 0);
});

test('browser chrome, pinch zoom and non-editable input do not masquerade as a keyboard', () => {
  const env = load();
  env.type();
  env.viewport.height = 744;
  env.viewportEvent('resize');
  env.frame();
  assert.equal(env.properties.get('--keyboard-inset'), '0px');
  env.viewport.height = 400;
  env.viewport.scale = 2;
  env.viewportEvent('resize');
  env.frame();
  assert.equal(env.properties.get('--keyboard-inset'), '0px');
  env.viewport.scale = 1;
  env.document.activeElement = { tagName: 'INPUT', type: 'range' };
  env.emit('focusin');
  env.frame();
  assert.equal(env.properties.get('--keyboard-inset'), '0px');
});

test('desktop fallback works without visualViewport and insecure origins do not register a worker', () => {
  const env = load({ visualViewport: false, secure: false });
  env.context.innerHeight = 650;
  env.emit('resize');
  env.frame();
  assert.equal(env.properties.get('--app-height'), '650px');
  assert.equal(env.properties.get('--viewport-top'), '0px');
  assert.deepEqual(env.registrations, []);
});
