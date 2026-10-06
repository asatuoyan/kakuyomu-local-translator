const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');

class Element {
    constructor() { this.children = []; this.textContent = ''; this.open = false; }
    append(...items) { this.children.push(...items); }
    replaceChildren() { this.children = []; this.textContent = ''; }
    showModal() { this.open = true; }
    close() { this.open = false; }
}

function setup(api) {
    const elements = new Map();
    const $ = id => {
        if (!elements.has(id)) elements.set(id, new Element());
        return elements.get(id);
    };
    const context = vm.createContext({$, api, loadedProject: 'book', examplesRequest: 0,
        document: {createElement: () => new Element()}});
    const source = fs.readFileSync(path.join(__dirname, '..', 'web', 'web_app.js'), 'utf8');
    vm.runInContext(source.slice(source.indexOf('async function showExamples('), source.indexOf('async function loadTerms(')), context);
    return {context, $};
}

test('opens a visible dialog immediately and displays paired saved examples', async () => {
    let resolve;
    const {context, $} = setup(() => new Promise(done => { resolve = done; }));
    const pending = context.showExamples('レオン');
    assert.equal($('examples').open, true);
    assert.match($('exampleContent').textContent, /正在查找/);
    resolve({examples: [{chapter: '第一章', original: 'レオンです', translation: '这是里昂'}]});
    await pending;
    assert.deepEqual($('exampleContent').children[0].children.map(item => item.textContent),
        ['第一章', '原文：レオンです', '译文：这是里昂']);
    $('closeExamples').onclick();
    assert.equal($('examples').open, false);
});

test('shows distinct empty-data reasons and request failures in the dialog', async () => {
    const missing = setup(async () => ({examples: [], reason: 'no_originals'}));
    await missing.context.showExamples('レオン');
    assert.match(missing.$('exampleContent').textContent, /尚未保存.*原文/);
    const unmatched = setup(async () => ({examples: [], reason: 'term_not_found'}));
    await unmatched.context.showExamples('レオン');
    assert.match(unmatched.$('exampleContent').textContent, /未找到此原词/);
    const failed = setup(async () => { throw Error('connection failed'); });
    await failed.context.showExamples('レオン');
    assert.match(failed.$('exampleContent').textContent, /查找例句失败：connection failed/);
});

test('older requests cannot replace the most recently selected term', async () => {
    const replies = [];
    const {context, $} = setup(() => new Promise(resolve => replies.push(resolve)));
    const first = context.showExamples('first');
    const second = context.showExamples('second');
    replies[1]({examples: [{chapter: 'two', original: 'second', translation: '二'}]});
    await second;
    replies[0]({examples: [{chapter: 'one', original: 'first', translation: '一'}]});
    await first;
    assert.match($('exampleTitle').textContent, /^second/);
    assert.equal($('exampleContent').children[0].children[1].textContent, '原文：second');
});

test('a late reply cannot reopen a closed dialog or show a previous project', async () => {
    let resolve;
    const {context, $} = setup(() => new Promise(done => { resolve = done; }));
    const pending = context.showExamples('term');
    $('closeExamples').onclick();
    resolve({examples: [{chapter: 'old', original: 'old', translation: '旧'}]});
    await pending;
    assert.equal($('examples').open, false);
    assert.equal($('exampleContent').children.length, 0);
    const next = context.showExamples('term');
    context.loadedProject = 'another-book';
    resolve({examples: [{chapter: 'old', original: 'old', translation: '旧'}]});
    await next;
    assert.equal($('exampleContent').children.length, 0);
});
