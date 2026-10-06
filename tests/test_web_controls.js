const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {test} = require('node:test');
const source = fs.readFileSync(require('node:path').join(__dirname, '..', 'web', 'web_app.js'), 'utf8');

function setup() {
    const elements = new Map();
    const $ = id => {
        if (!elements.has(id)) elements.set(id, {value: '', textContent: '', options: [1], replaceChildren() {}, append() {}});
        return elements.get(id);
    };
    $('model').value = 'model'; $('url').value = 'https://kakuyomu.jp/works/123';
    const calls = [];
    const context = vm.createContext({$, modelsLoading: false, taskActionPending: false,
        acquisitionActionPending: false, resultFingerprint: '', document: {querySelectorAll: () => []},
        state: {task: {running: false, percent: 0, message: 'ready'}, acquisition_task: {running: false}, acquisition_busy: false},
        showQueueSubmission: result => { context.lastSubmission = result; },
        notice: text => { context.lastNotice = text; }});
    context.api = async (route, body) => {
        calls.push({route, body});
        if (route === 'start') context.state.task = {running: true, percent: 0};
        if (route === 'stop') context.state.task.stopping = true;
        if (route === 'acquire') { context.state.acquisition_task = {running: true}; context.state.acquisition_busy = true; }
        if (route === 'stop-acquisition') context.state.acquisition_task.stopping = true;
        return {};
    };
    vm.runInContext(source.slice(source.indexOf('function renderStatus()'), source.indexOf('async function poll()')), context);
    vm.runInContext(source.slice(source.indexOf('function renderAcquisition()'), source.indexOf('function filterTerms()')), context);
    vm.runInContext(source.slice(source.indexOf("$('startForm').onsubmit"), source.indexOf("$('save').onclick")), context);
    context.poll = async () => { context.renderStatus(); context.renderAcquisition(); };
    context.renderStatus(); context.renderAcquisition();
    return {context, $, calls, submit: () => $('startForm').onsubmit({preventDefault() {}})};
}

test('translation submissions always queue and allow adding another book while busy', async () => {
    const {context, $, calls, submit} = setup();
    assert.equal($('start').textContent, '获取并翻译');
    await submit();
    context.state.task.running = true; context.renderStatus();
    assert.equal($('start').textContent, '加入翻译队列');
    assert.equal($('start').disabled, false);
    $('model').value = ''; context.modelsLoading = true; context.renderStatus();
    assert.equal($('start').disabled, true, 'a new task requires a model');
    $('model').value = 'model'; context.modelsLoading = false;
    await submit();
    assert.deepEqual(calls.map(call => call.route), ['enqueue', 'enqueue']);
    assert.equal(calls[0].body.slot, 'translation');
    context.state.task.running = false; context.renderStatus();
    assert.equal($('start').textContent, '获取并翻译');
});

test('independent acquisition always queues and never stops either active task', async () => {
    const {context, $, calls} = setup();
    context.state.task.running = true;
    context.state.acquisition_task.running = true;
    context.renderAcquisition();
    await $('acquireOnly').onclick();
    assert.equal($('acquireOnly').textContent, '加入获取队列');
    assert.equal($('acquireOnly').disabled, false);
    await $('acquireOnly').onclick();
    assert.equal(context.state.task.running, true);
    assert.equal(context.state.acquisition_task.running, true);
    assert.deepEqual(calls.map(call => call.route), ['enqueue', 'enqueue']);
    assert.equal(calls[0].body.slot, 'acquisition');
});


test('pending requests suppress double clicks and rejected requests restore the button', async () => {
    const {context, $, submit} = setup();
    let release, calls = 0;
    context.api = () => { calls++; return new Promise(resolve => { release = resolve; }); };
    const first = submit(); await submit();
    assert.equal(calls, 1); assert.equal($('start').disabled, true);
    release({}); await first;
    assert.equal($('start').disabled, false);
    context.api = async () => { throw Error('failed'); };
    await $('acquireOnly').onclick();
    assert.equal(context.lastNotice, 'failed');
    assert.equal($('acquireOnly').disabled, false);
});

test('pending recovery does not repurpose new-task submission buttons', async () => {
    const {context, $, submit} = setup();
    const routes=[];
    context.state.task.recovering = true;
    context.state.acquisition_task.recovering = true;
    context.api = async route => {
        routes.push(route);
        if(route === 'stop') context.state.task.recovering = false;
        if(route === 'stop-acquisition') context.state.acquisition_task.recovering = false;
        return {};
    };
    context.renderStatus(); context.renderAcquisition();
    assert.equal($('start').disabled, false);
    assert.equal($('acquireOnly').disabled, false);
    await submit();
    await $('acquireOnly').onclick();
    assert.deepEqual(routes, ['enqueue', 'enqueue']);
    assert.equal(context.state.task.recovering, true);
    assert.equal(context.state.acquisition_task.recovering, true);
});

test('paused queue still accepts new jobs without launching them', () => {
    const {context, $} = setup();
    context.state.queue_paused = true;
    context.renderStatus(); context.renderAcquisition();
    assert.equal($('start').disabled, false);
    assert.equal($('acquireOnly').disabled, false);
    context.state.task.running = true;
    context.renderStatus();
    assert.equal($('start').disabled, false);
});
