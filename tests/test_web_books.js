const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {test} = require('node:test');
const source = fs.readFileSync(require('node:path').join(__dirname, '..', 'web', 'web_app.js'), 'utf8');

class Element {
    constructor() {
        this.children = []; this.value = ''; this.textContent = '';
        this.attributes = {};
        this.classes = new Set();
        this.classList = {toggle: (name, enabled) => enabled ? this.classes.add(name) : this.classes.delete(name)};
    }
    append(...items) { this.children.push(...items); }
    replaceChildren(...items) { this.children = items; this.textContent = ''; }
    setAttribute(name, value) { this.attributes[name] = value; }
    get options() { return this.children; }
}

function setup(projects) {
    const elements = new Map();
    const $ = id => {
        if (!elements.has(id)) elements.set(id, new Element());
        return elements.get(id);
    };
    $('bookFilter').value = 'all';
    $('model').value = 'installed';
    const context = vm.createContext({
        $, state: {projects, task: {running: false}},
        booksPage: 1, booksPageSize: 10, booksFingerprint: '', projectsFingerprint: '',
        manualProject: false, dirty: false, modelsLoading: false,
        document: {activeElement: null, createElement: () => new Element(),
            querySelectorAll: () => []},
        Option: function(text, value) { this.textContent = text; this.value = value; },
        button: (text, action) => Object.assign(new Element(), {textContent: text, onclick: action}),
        downloadLink: () => Object.assign(new Element(), {textContent: '下载 EPUB'})
    });
    vm.runInContext(source.slice(source.indexOf('function renderBooks()'), source.indexOf('function renderStatus()')), context);
    context.renderBooks();
    return {context, $};
}

const projects = Array.from({length: 23}, (_, index) => ({
    id: `book-${index}`, title: `Novel ${index}`, language: 'en', chapters: index,
    completed: index % 2 === 0, resume: {}, download: 'book.epub'
}));

test('ten books per page, navigation and all project options remain available', () => {
    const {$} = setup(projects);
    assert.equal($('books').children.length, 10);
    assert.equal($('project').children.length, 23);
    assert.equal($('booksPrevious').disabled, true);
    $('booksNext').onclick();
    assert.equal($('books').children[0].children[0].children[0].textContent, 'Novel 10');
    $('booksNext').onclick();
    assert.equal($('books').children.length, 3);
    assert.equal($('booksNext').disabled, true);
    assert.match($('booksPagination').textContent, /23/);
    assert.deepEqual($('books').children[0].children[1].children.map(item => item.textContent),
        ['阅读', '术语', '继续翻译', '下载 EPUB']);
});

test('search and completion filters reset the page and handle no matches', () => {
    const {$} = setup(projects);
    $('booksNext').onclick();
    $('bookFilter').value = 'unfinished';
    $('bookFilter').onchange();
    assert.equal($('booksPrevious').disabled, true);
    assert.match($('booksPagination').textContent, /11/);
    $('bookSearch').value = ' NOVEL 21 ';
    $('bookSearch').oninput();
    assert.equal($('books').children.length, 1);
    $('bookFilter').value = 'completed';
    $('bookFilter').onchange();
    assert.equal($('books').children.length, 0);
    assert.equal($('books').textContent, '没有符合条件的作品。');
    assert.equal($('booksNext').disabled, true);
});

test('polling preserves search and clamps a page when projects disappear', () => {
    const {context, $} = setup([...projects]);
    $('booksNext').onclick();
    $('booksNext').onclick();
    context.state.projects = projects.slice(0, 2);
    context.renderBooks();
    assert.equal($('books').children.length, 2);
    assert.equal($('booksPrevious').disabled, true);
    $('bookSearch').value = 'absent';
    $('bookSearch').oninput();
    context.renderBooks();
    assert.equal($('bookSearch').value, 'absent');
    assert.equal($('books').children.length, 0);
});

test('three-page navigation preserves the running task and book search', () => {
    const {context, $} = setup([...projects]);
    let termsLoads = 0;
    context.loadTerms = async () => { termsLoads++; };
    vm.runInContext(source.slice(source.indexOf('function tab('), source.indexOf('async function read(')), context);
    context.state.task.running = true;
    $('bookSearch').value = 'Novel';
    $('booksNext').onclick();
    $('translateTab').onclick();
    assert.equal($('translatePage').hidden, false);
    assert.equal($('booksPage').hidden, true);
    $('booksTab').onclick();
    assert.equal($('translatePage').hidden, true);
    assert.equal($('booksPage').hidden, false);
    assert.equal($('termsPage').hidden, true);
    assert.equal($('booksTab').attributes['aria-pressed'], 'true');
    $('termsTab').onclick();
    assert.equal($('termsPage').hidden, false);
    assert.equal(termsLoads, 1);
    $('booksTab').onclick();
    assert.equal($('bookSearch').value, 'Novel');
    assert.equal(context.booksPage, 2);
    assert.equal(context.state.task.running, true);
});

test('completion keeps the current page and offers a books-page entry', () => {
    const {context, $} = setup([...projects]);
    context.resultFingerprint = '';
    context.loadTerms = async () => {};
    vm.runInContext(source.slice(source.indexOf('function tab('), source.indexOf('async function read(')), context);
    vm.runInContext(source.slice(source.indexOf('function renderStatus()'), source.indexOf('async function poll()')), context);
    $('translateTab').onclick();
    context.state.task.output = 'C:\\output\\book.epub';
    context.state.task.project = 'book-0';
    context.renderStatus();
    assert.equal($('translatePage').hidden, false);
    const actions = $('result').children;
    assert.deepEqual(actions.map(item => item.textContent), ['下载 EPUB', '阅读译文', '查看我的作品']);
    actions[2].onclick();
    assert.equal($('booksPage').hidden, false);
    assert.equal($('translatePage').hidden, true);
});
