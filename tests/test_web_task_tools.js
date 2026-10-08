const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');
const app = fs.readFileSync(path.join(__dirname, '..', 'web', 'web_app.js'), 'utf8');
const reader = fs.readFileSync(path.join(__dirname, '..', 'web', 'reader.html'), 'utf8');

test('user interaction resets idle countdown while synthetic events do not', () => {
    const tasks = fs.readFileSync(path.join(__dirname, '..', 'web', 'web_tasks.js'), 'utf8');
    const listeners = new Map(), requests = [];
    let now = 1000;
    const context = vm.createContext({Date: {now: () => now},
        document: {addEventListener: (event, handler) => listeners.set(event, handler)},
        api: (route, body) => { requests.push({route, body}); return Promise.resolve({}); }});
    vm.runInContext(tasks.slice(tasks.indexOf('let lastIdleActivity'), tasks.indexOf('const pauseQueue')), context);
    listeners.get('scroll')({isTrusted: false});
    assert.equal(requests.length, 0);
    listeners.get('pointerdown')({isTrusted: true});
    assert.equal(requests[0].route, 'activity');
    listeners.get('input')({isTrusted: true});
    assert.equal(requests.length, 1);
    now += 1000;
    listeners.get('keydown')({isTrusted: true});
    assert.equal(requests.length, 2);
});

test('dynamic queue buttons never submit their surrounding task form', () => {
    const context = vm.createContext({document: {createElement: () => ({})}});
    vm.runInContext(app.slice(app.indexOf('function button('), app.indexOf('function moreActions(')), context);
    assert.equal(context.button('queue', () => {}).type, 'button');
});

test('hidden application lowers polling to thirty seconds and foreground restores normal interval', async () => {
    const timers = [];
    const context = vm.createContext({state: null, dirty: false, pollTimer: null,
        document: {hidden: true}, api: async () => ({}), $: () => ({textContent: '', hidden: true}),
        notice() {}, renderStatus() {}, renderBooks() {}, renderRecent() {}, renderAcquisition() {},
        clearTimeout() {}, setTimeout: (_, delay) => timers.push(delay)});
    vm.runInContext(app.slice(app.indexOf('async function poll('), app.indexOf('function renderAcquisition(')), context);
    await context.poll(); assert.equal(timers.at(-1), 30000);
    context.document.hidden = false;
    await context.poll(); assert.equal(timers.at(-1), 2500);
});

test('paragraph anchor retains index and offset after layout changes and accepts old pixel bookmarks', () => {
    const context = vm.createContext({scrollY: 0, restoring: false, requestAnimationFrame: action => action()});
    const positions = [0, 100, 200];
    const nodes = positions.map((_, index) => ({dataset: {paragraph: String(index)},
        getBoundingClientRect: () => ({top: positions[index] - context.scrollY, bottom: positions[index] + 100 - context.scrollY})}));
    context.$ = () => ({querySelectorAll: () => nodes,
        querySelector: selector => nodes[Number(selector.match(/"(\d+)"/)[1])]});
    context.document = {querySelector: () => ({getBoundingClientRect: () => ({bottom: 100})})};
    context.scrollTo = (_, y) => { context.scrollY = y; };
    vm.runInContext(reader.slice(reader.indexOf('function capturePosition('), reader.indexOf('function render(')), context);
    const anchor = context.capturePosition();
    assert.equal(anchor.paragraph, 1); assert.equal(anchor.offset, 8);
    positions[1] = 300; positions[2] = 400;
    context.restorePosition(anchor);
    assert.equal(context.scrollY, 200);
    const restored = context.capturePosition();
    assert.equal(restored.paragraph, 1); assert.equal(restored.offset, 8);
    context.restorePosition(123); assert.equal(context.scrollY, 123);
});

test('background update checks also lower frequency and visibility restores it', async () => {
    const updates = fs.readFileSync(path.join(__dirname, '..', 'web', 'web_updates.js'), 'utf8');
    const timers = [];
    const context = vm.createContext({updateState: null, updateTimer: null, updateReloading: false,
        document: {hidden: true}, loadedServerInstance: 'same', dirty: false,
        api: async () => ({instance: 'same'}), renderUpdates() {}, clearTimeout() {},
        setTimeout: (_, delay) => timers.push(delay)});
    vm.runInContext(updates.slice(updates.indexOf('async function checkUpdates('), updates.indexOf('async function restartForUpdate(')), context);
    await context.checkUpdates(); assert.equal(timers.at(-1), 30000);
    context.document.hidden = false;
    await context.checkUpdates(); assert.equal(timers.at(-1), 3000);
});
