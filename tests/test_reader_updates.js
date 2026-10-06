const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');

test('chapter notices count only later additions and reset for a different book', () => {
    const html = fs.readFileSync(path.join(__dirname, '..', 'web', 'reader.html'), 'utf8');
    const context = vm.createContext({});
    vm.runInContext(html.slice(html.indexOf('let observedChapterIds'), html.indexOf('function read(')), context);
    const count = () => vm.runInContext('newChapterCount', context);
    context.chapterUpdates(['1', '2']);
    assert.equal(count(), 0);
    context.chapterUpdates(['1', '2', '3', '4']);
    assert.equal(count(), 2);
    context.chapterUpdates(['1', '2', '3', '4']);
    assert.equal(count(), 2);
    context.chapterUpdates(['1', '2', '3', '4', '5']);
    assert.equal(count(), 3);
    context.chapterUpdates(['1'], true);
    assert.equal(count(), 0);
});

test('reader polling discovers appended chapters and refreshes text without selecting another chapter', async () => {
    const html = fs.readFileSync(path.join(__dirname, '..', 'web', 'reader.html'), 'utf8');
    const elements = new Map();
    const $ = id => {
        if (!elements.has(id)) elements.set(id, {options: [], textContent: '', value: '', replaceChildren() { this.options=[]; }, append(item) { this.options.push(item); }});
        return elements.get(id);
    };
    let catalog = {book: 'novel', chapters: [{id: '1', title: 'one', revision: 1}]};
    let body = {title: 'one', translations: ['first'], originals: [], revision: 1};
    const renders = [], requests = [];
    const context = vm.createContext({$, base: '/book/', book: '', current: '', revision: -1, record: null,
        catalogSignature: '', catalogChapters: [], newChapterCount: 0,
        read: (_, fallback) => fallback, save() {}, chapterUpdates() {}, buildDirectory() {}, navigation() {},
        render: restore => renders.push(restore), AbortController,
        document: {createElement: () => ({})}, setTimeout: () => 1, clearTimeout() {},
        fetch: async (url, options) => { requests.push({url, options}); return {ok: true, json: async () => url.endsWith('catalog') ? catalog : body}; }});
    vm.runInContext(html.slice(html.indexOf('let pending='), html.indexOf("addEventListener('focus'")), context);
    await context.poll();
    catalog = {book: 'novel', chapters: [{id: '1', title: 'one', revision: 2}, {id: '2', title: 'two', revision: 2}]};
    body = {...body, translations: ['updated'], revision: 2};
    await context.poll();
    assert.equal($('chapters').options.length, 2);
    assert.equal(context.current, '1');
    assert.equal(context.record.translations[0], 'updated');
    assert.deepEqual(renders, [true, false]);
    assert.ok(requests.every(request => request.options.cache === 'no-store'));
});

test('stalled reader request aborts and releases pending polling so it can reconnect', async () => {
    const html = fs.readFileSync(path.join(__dirname, '..', 'web', 'reader.html'), 'utf8');
    const timers = [];
    const status = {};
    const context = vm.createContext({$: () => status, base: '/book/', AbortController,
        setTimeout: (callback, delay) => { timers.push({callback, delay}); return timers.length; }, clearTimeout() {},
        fetch: (_, options) => new Promise((resolve, reject) => options.signal.addEventListener('abort', () => reject(Error('timeout'))))});
    vm.runInContext(html.slice(html.indexOf('let pending='), html.indexOf("addEventListener('focus'")), context);
    const pending = context.poll();
    assert.equal(vm.runInContext('pending', context), true);
    await context.poll();
    timers.find(timer => timer.delay === 10000).callback();
    await pending;
    assert.equal(vm.runInContext('pending', context), false);
    assert.equal(timers.at(-1).delay, 0, 'retry a poll requested while the previous request was stalled');
});
