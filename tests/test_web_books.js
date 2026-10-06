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
    addEventListener() {}
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
        booksPage: 1, booksPageSize: 5, booksFingerprint: '', projectsFingerprint: '',
        manualProject: false, dirty: false, modelsLoading: false, taskActionPending: false,
        document: {activeElement: null, createElement: () => new Element(),
            querySelectorAll: () => []},
        Option: function(text, value) { this.textContent = text; this.value = value; },
        button: (text, action) => Object.assign(new Element(), {textContent: text, onclick: action}),
        downloadLink: (_, label = '下载 EPUB') => Object.assign(new Element(), {textContent: label})
    });
    vm.runInContext(source.slice(source.indexOf('function moreActions('), source.indexOf("document.addEventListener('click'")), context);
    vm.runInContext(source.slice(source.indexOf('function renderBooks()'), source.indexOf('function renderStatus()')), context);
    context.renderBooks();
    return {context, $};
}

const projects = Array.from({length: 23}, (_, index) => ({
    id: `book-${index}`, title: `Novel ${index}`, language: 'en', chapters: index,
    completed: index % 2 === 0, resume: {}, download: 'book.epub'
}));

test('network books can queue incremental updates with or without follow-up translation', async () => {
    const {context, $} = setup([{id: 'book', title: 'fixture', kind: 'source', completed: true,
        update_url: 'https://kakuyomu.jp/works/123', source_download: 'original.epub', language: 'original'}]);
    $('language').value = 'en';
    const calls = [];
    context.api = async (route, body) => { calls.push({route, body}); return {}; };
    context.tab = () => {}; context.notice = () => {}; context.poll = async () => {};
    const actions = $('books').children[0].children[1].children.at(-1).children[1].children;
    await actions.find(item => item.className === 'checkUpdates').onclick();
    await actions.find(item => item.className === 'checkSourceUpdates').onclick();
    assert.equal(calls[0].route, 'check-book-updates');
    assert.equal(calls[0].body.translate, true);
    assert.equal(calls[0].body.language, 'en');
    assert.equal(calls[1].body.translate, false);
});

test('five books per page, navigation and all project options remain available', () => {
    const {$} = setup(projects);
    assert.equal($('books').children.length, 5);
    assert.equal($('project').children.length, 23);
    assert.equal($('booksPrevious').disabled, true);
    $('booksNext').onclick();
    assert.equal($('books').children[0].children[0].children[0].textContent, 'Novel 5');
    $('booksNext').onclick();
    $('booksNext').onclick();
    $('booksNext').onclick();
    assert.equal($('books').children.length, 3);
    assert.equal($('booksNext').disabled, true);
    assert.match($('booksPagination').textContent, /23/);
    assert.deepEqual($('books').children[0].children[1].children.map(item => item.textContent),
        ['阅读', '继续翻译', '']);
    const menu = $('books').children[0].children[1].children.at(-1);
    assert.equal(menu.children[0].textContent, '更多');
    assert.equal(menu.children[1].children[0].textContent, '下载 EPUB');
    assert.doesNotMatch($('books').children[0].children[0].children[1].textContent, /术语/);
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
    assert.deepEqual(actions.map(item => item.textContent), ['下载 EPUB', '阅读译文', '查看小说书库']);
    actions[2].onclick();
    assert.equal($('booksPage').hidden, false);
    assert.equal($('translatePage').hidden, true);
});

test('acquired originals provide download and translation without unavailable reader actions', () => {
    const { $ } = setup([{id: 'source', kind: 'source', title: 'Original', acquired_chapters: 10,
        total_chapters: 10, chapters: 0, completed: true, source_download: 'source.epub',
        resume: {source: 'source.epub'}}]);
    const row = $('books').children[0];
    assert.match(row.children[0].children[1].textContent, /10\/10.*尚未翻译/);
    assert.deepEqual(row.children[1].children.map(item => item.textContent), ['开始翻译', '']);
    assert.equal(row.children[1].children.at(-1).children[1].children[0].textContent, '下载原文');
});

test('translating books expose completed original independently of translated EPUB', () => {
    const { $ } = setup([{id: 'book', title: 'Translation', acquired_chapters: 10, total_chapters: 10,
        chapters: 2, completed: false, source_download: 'original.epub'}]);
    const row = $('books').children[0];
    assert.match(row.children[0].children[1].textContent, /已译 2\/10.*原文已获取 10\/10/);
    assert.deepEqual(row.children[1].children.map(item => item.textContent), ['阅读', '']);
    assert.equal(row.children[1].children.at(-1).children[1].children[0].textContent, '下载原文');
});
