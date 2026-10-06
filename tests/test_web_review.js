const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {test} = require('node:test');
const source = fs.readFileSync(require('node:path').join(__dirname, '..', 'web', 'web_review.js'), 'utf8');

class Element {
    constructor() { this.children = []; this.value = ''; this.style = {}; this.dataset = {}; this.parentElement = this; }
    append(...items) { this.children.push(...items); }
    replaceChildren() { this.children = []; }
    scrollIntoView() {}
}
function setup(api) {
    const elements = new Map();
    const $ = id => {
        if (!elements.has(id)) elements.set(id, new Element());
        return elements.get(id);
    };
    $('model').value = 'installed-model';
    const context = vm.createContext({$, api, dirty: false, loadedProject: 'book',
        state: {task: {running: false}, projects: [{id: 'book', title: 'Title'}]},
        document: {createElement: () => new Element(), createTextNode: text => ({textContent: text}), querySelectorAll: () => []},
        button: (text, onclick) => Object.assign(new Element(), {textContent: text, onclick}),
        tab: () => {}, notice: text => { context.lastNotice = text; }, poll: async () => {}});
    vm.runInContext(source, context);
    return {context, $, get: expression => vm.runInContext(expression, context)};
}
const report = {term_revision: 'revision', mode: 'changes', unavailable: [], items:
    Array.from({length: 53}, (_, index) => ({id: String(index), chapter_index: 1,
        chapter: 'Chapter', paragraph: index + 1, original: `original ${index}`, translation: `old ${index}`, reasons: ['Old name']}))};

test('review renders fifty rows and preserves selections across pages', async () => {
    const {context, $, get} = setup(async () => report);
    await context.openReview('book', 'changes');
    assert.equal($('reviewRows').children.length, 50);
    $('reviewSelectPage').onclick(); $('reviewNext').onclick();
    assert.equal($('reviewRows').children.length, 3);
    const checkbox = $('reviewRows').children[0].children[0].children[0];
    checkbox.checked = true; checkbox.onchange();
    assert.equal(get('reviewSelected.size'), 51);
    $('reviewPrevious').onclick();
    assert.equal($('reviewRows').children[0].children[0].children[0].checked, true);
    $('reviewClear').onclick();
    assert.equal(get('reviewSelected.size'), 0);
    assert.equal($('reviewRetranslate').disabled, true);
});

test('late scan response cannot replace a newly selected book', async () => {
    const resolvers = [];
    const {context, get} = setup(() => new Promise(resolve => resolvers.push(resolve)));
    const first = context.openReview('first');
    const second = context.openReview('second');
    resolvers[1]({...report, items: []}); await second;
    resolvers[0](report); await first;
    assert.equal(get('reviewProject'), 'second');
    assert.equal(get('reviewData.items.length'), 0);
    assert.equal(get('reviewLoading'), false);
});

test('retranslation sends only selected paragraphs with glossary revision and model', async () => {
    let submitted;
    const {context, $} = setup(async (route, body) => {
        if (route === 'retranslate') { submitted = body; return {started: true}; }
        return report;
    });
    await context.openReview('book', 'changes');
    const checkbox = $('reviewRows').children[1].children[0].children[0];
    checkbox.checked = true; checkbox.onchange();
    await $('reviewRetranslate').onclick();
    assert.equal(submitted.items.length, 1);
    assert.equal(submitted.items[0].paragraph, 2);
    assert.equal(submitted.term_revision, 'revision');
    assert.equal(submitted.model, 'installed-model');
});

test('pending glossary edits and active tasks prevent starting review', async () => {
    let calls = 0;
    const {context, $} = setup(async () => { calls++; return report; });
    context.dirty = true; await context.openReview('book');
    assert.equal(calls, 0); assert.match(context.lastNotice, /保存/);
    context.dirty = false; context.state.task.running = true;
    await context.openReview('book'); assert.equal(calls, 0);
    context.renderReviewState();
    assert.equal($('reviewRetranslate').disabled, true);
    assert.equal($('reviewRetranslate').disabled, true);
});

test('review button switches to stop for its own running task even without a selection', async () => {
    const calls = [];
    const {context, $} = setup(async route => { calls.push(route); return {}; });
    vm.runInContext("reviewProject = 'book'", context);
    context.state.task = {running: true, kind: 'review', project: 'book', percent: 20};
    context.renderReviewState();
    assert.equal($('reviewRetranslate').textContent, '停止重译');
    assert.equal($('reviewRetranslate').disabled, false);
    await $('reviewRetranslate').onclick();
    assert.deepEqual(calls, ['stop']);
    context.state.task.stopping = true; context.renderReviewState();
    assert.equal($('reviewRetranslate').textContent, '正在停止重译…');
    assert.equal($('reviewRetranslate').disabled, true);
    context.state.task.running = false; context.renderReviewState();
    assert.match($('reviewRetranslate').textContent, /重译选中段落/);
});
