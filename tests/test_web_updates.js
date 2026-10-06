const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {test} = require('node:test');
const source = fs.readFileSync(require('node:path').join(__dirname, '..', 'web', 'web_updates.js'), 'utf8');

function setup() {
    const elements = new Map(), storage = new Map(), calls = [];
    const $ = id => {
        if (!elements.has(id)) elements.set(id, {value: id, hidden: id.endsWith('Page') && id !== 'booksPage'});
        return elements.get(id);
    };
    let response = {frontend_revision: 'front', instance: 'server', backend_changed: false,
        restart_pending: false, restart_supported: true, busy: false};
    const context = vm.createContext({$, token: 'local', dirty: false, state: {task: {running: false}}, modelsLoading: false,
        document: {querySelector: selector => ({content: selector.includes('frontend') ? 'front' : 'server'})},
        sessionStorage: {getItem: key => storage.get(key) || null, setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key)},
        location: {reload: () => { context.reloads++; }}, reloads: 0,
        setTimeout: () => 1, clearTimeout: () => {}, notice: text => { context.lastNotice = text; },
        tab: page => { context.currentPage = page; }, renderBooks: () => {}, renderStatus: () => {},
        api: async (route, body) => { calls.push({route, body}); return response; }});
    vm.runInContext(source.slice(0, source.lastIndexOf('restoreUpdateState();')), context);
    return {context, $, storage, calls, setResponse: data => { response = {...response, ...data}; }};
}

test('frontend-only update offers refresh and refuses to discard unsaved glossary edits', async () => {
    const {context, $, storage, setResponse} = setup();
    setResponse({frontend_revision: 'new front'}); await context.checkUpdates();
    assert.equal($('updatesPanel').hidden, false);
    assert.equal($('updateRefresh').hidden, false);
    assert.equal($('updateRestart').hidden, true);
    context.dirty = true; context.refreshUpdatedPage();
    assert.equal(context.reloads, 0); assert.match(context.lastNotice, /保存/);
    context.dirty = false; context.refreshUpdatedPage();
    assert.equal(context.reloads, 1);
    const saved = JSON.parse(storage.get('web-update-state:local'));
    assert.equal(saved.page, 'books'); assert.equal(saved.values.bookSearch, 'bookSearch');
});

test('busy backend update offers wait or stop instead of immediate restart', async () => {
    const {context, $, calls, setResponse} = setup();
    setResponse({backend_changed: true, busy: true}); await context.checkUpdates();
    assert.equal($('updateRestart').hidden, true);
    assert.equal($('updateWait').hidden, false); assert.equal($('updateStop').hidden, false);
    setResponse({restart_pending: true}); await $('updateWait').onclick();
    assert.equal(calls.find(call => call.route === 'restart').body.mode, 'wait');
    assert.equal($('updateCancel').hidden, false);
    assert.equal(context.reloads, 0);
});

test('new server instance reconnects by refreshing only after drafts have been saved', async () => {
    const {context, $, setResponse} = setup();
    context.dirty = true; setResponse({instance: 'restarted'}); await context.checkUpdates();
    assert.equal(context.reloads, 0); assert.equal($('updateRefresh').disabled, true);
    context.dirty = false; await context.checkUpdates();
    assert.equal(context.reloads, 1);
});

test('restores input values and selected page after initial projects and models load', () => {
    const {context, $, storage} = setup();
    storage.set('web-update-state:local', JSON.stringify({values: {url: 'saved URL', model: 'installed', project: 'book'}, page: 'terms'}));
    context.modelsLoading = true; context.restoreUpdateState();
    assert.equal(storage.size, 1);
    context.modelsLoading = false; context.restoreUpdateState();
    assert.equal(storage.size, 0); assert.equal($('url').value, 'saved URL');
    assert.equal($('model').value, 'installed'); assert.equal(context.currentPage, 'terms');
    assert.equal(context.manualProject, true);
});
