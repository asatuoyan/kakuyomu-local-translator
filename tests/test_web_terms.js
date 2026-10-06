const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');
const source = fs.readFileSync(path.join(__dirname, '..', 'web', 'web_app.js'), 'utf8');

class Element {
    constructor() { this.children = []; this.value = ''; this.textContent = ''; this.dataset = {}; }
    append(...items) { this.children.push(...items); }
    replaceChildren() { this.children = []; this.textContent = ''; }
    setAttribute() {}
    close() {}
}
function setup() {
    const elements = new Map();
    const $ = id => {
        if (!elements.has(id)) elements.set(id, new Element());
        return elements.get(id);
    };
    $('project').value = 'book';
    const entries = Array.from({length: 103}, (_, index) => ({source: `term ${index}`, target: `name ${index}`, category: 'people'}));
    const context = vm.createContext({$, entries, termEdits: new Map(), dirty: false,
        termsPage: 1, termsPageSize: 20, loadedProject: 'book', termsLoading: false,
        termsRevision: 'initial', examplesRequest: 0,
        document: {createElement: () => new Element()},
        button: (text, onclick) => Object.assign(new Element(), {textContent: text, onclick})});
    vm.runInContext(source.slice(source.indexOf('function filterTerms()'), source.indexOf('async function showExamples(')), context);
    vm.runInContext(source.slice(source.indexOf('async function loadTerms()'), source.indexOf("$('startForm').onsubmit")), context);
    context.filterTerms();
    return {context, $};
}
test('renders only twenty terms and retains edits across pages and full-list searches', () => {
    const {context, $} = setup();
    assert.equal($('termRows').children.length, 20);
    const firstInput = $('termRows').children[0].children[1].children[0];
    firstInput.value = 'edited name';
    firstInput.oninput();
    $('termsNext').onclick();
    const secondInput = $('termRows').children[0].children[1].children[0];
    secondInput.value = 'other page edit';
    secondInput.oninput();
    for (let page = 2; page < 6; page++) $('termsNext').onclick();
    assert.equal($('termRows').children.length, 3);
    assert.equal($('termsNext').disabled, true);
    $('termSearch').value = ' TERM 102 ';
    $('termSearch').oninput();
    assert.equal($('termRows').children.length, 1);
    assert.equal($('termRows').children[0].dataset.index, 102);
    assert.equal(context.termsPage, 1);
    $('termSearch').value = '';
    $('termSearch').oninput();
    assert.equal($('termRows').children[0].children[1].children[0].value, 'edited name');
    assert.equal(context.termEdits.get('term 20').target, 'other page edit');
    assert.equal(context.termEdits.size, 2);
    assert.equal(context.dirty, true);
});
test('polling never replaces pending edits and empty searches disable navigation', async () => {
    const {context, $} = setup();
    const input = $('termRows').children[0].children[1].children[0];
    input.value = 'draft'; input.oninput();
    let reads = 0;
    context.api = async () => { reads++; return {}; };
    await context.loadTerms();
    assert.equal(reads, 0);
    assert.equal(context.entries[0].target, 'draft');
    $('termSearch').value = 'absent'; $('termSearch').oninput();
    assert.equal($('termRows').children.length, 0);
    assert.equal($('termsPrevious').disabled, true);
    assert.equal($('termsNext').disabled, true);
    assert.equal($('emptyTerms').hidden, false);
});
test('saving submits edited terms from every page as a merge', async () => {
    const {context, $} = setup();
    const edit = value => {
        const input = $('termRows').children[0].children[1].children[0];
        input.value = value; input.oninput();
    };
    edit('first draft'); $('termsNext').onclick(); edit('second draft');
    let body;
    context.api = async (_, data) => { body = data; return {saved: 103}; };
    context.loadTerms = async () => {};
    context.notice = () => {};
    vm.runInContext(source.slice(source.indexOf("$('save').onclick"), source.indexOf("$('export').onclick")), context);
    await $('save').onclick();
    assert.equal(body.merge, true);
    assert.deepEqual(Array.from(body.entries, entry => entry.source), ['term 0', 'term 20']);
    assert.equal(context.dirty, false);
});
