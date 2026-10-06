const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {test} = require('node:test');
const source = fs.readFileSync(require('node:path').join(__dirname, '..', 'web', 'web_app.js'), 'utf8');

test('connection loss reports uncertain task acceptance without retrying a mutation', async () => {
    let calls = 0;
    const context = vm.createContext({token: 'local', fetch: async () => { calls++; throw TypeError('Failed to fetch'); }});
    vm.runInContext(source.slice(source.indexOf('async function api('), source.indexOf('function button(')), context);
    await assert.rejects(context.api('acquire', {url: 'example'}), /\u4efb\u52a1\u662f\u5426\u542f\u52a8\u5c1a\u672a\u786e\u8ba4/);
    assert.equal(calls, 1);
});

test('successful status recovery clears the stale connection notice', async () => {
    const noticeElement = {textContent: '\u8fde\u63a5\u5931\u8d25\uff1aFailed to fetch'};
    const context = vm.createContext({state: null, dirty: false, pollTimer: null,
        api: async () => ({task: {running: false}}),
        $: id => id === 'notice' ? noticeElement : {hidden: true},
        notice: text => { noticeElement.textContent = text; },
        renderStatus() {}, renderBooks() {}, renderRecent() {}, renderAcquisition() {},
        clearTimeout() {}, setTimeout() { return 1; }});
    vm.runInContext(source.slice(source.indexOf('async function poll('), source.indexOf('function renderAcquisition(')), context);
    await context.poll();
    assert.equal(noticeElement.textContent, '');
});
