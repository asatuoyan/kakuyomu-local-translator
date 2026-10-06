const $ = id => document.getElementById(id);
const token = location.pathname.split('/')[1];
let state = null, entries = [], loadedProject = '', dirty = false, manualProject = false;
let modelsLoading = false, termsRevision = '', termsLoading = false;
let projectsFingerprint = '', recentFingerprint = '', resultFingerprint = '';
let booksFingerprint = '', booksPage = 1;
let examplesRequest = 0;
let termsPage = 1;
const termsPageSize = 20;
const termEdits = new Map();
const booksPageSize = 5;
let taskActionPending = false, acquisitionActionPending = false, pollTimer = null;

function notice(message) {
    $('notice').textContent = message;
    $('notice').hidden = !message;
}

async function api(path, body) {
    let response;
    try {
        response = await fetch('api/' + path, body === undefined ? {} : {
            method: 'POST', headers: {'Content-Type': 'application/json', 'X-Local-App': token},
            body: JSON.stringify(body)
        });
    } catch (error) {
        throw Error(body === undefined ? '无法连接本地服务，请确认启动窗口仍在运行；若重新启动过程序，请使用启动窗口显示的新网址' :
            '连接失败：未收到本地服务响应，任务是否启动尚未确认。请等待进度恢复后再操作；若重新启动过程序，请使用启动窗口显示的新网址');
    }
    const data = await response.json();
    if (!response.ok) throw Error(data.error || '请求失败');
    return data;
}

function button(label, action) {
    const item = document.createElement('button');
    item.type = 'button';
    item.textContent = label;
    item.onclick = () => Promise.resolve().then(action).catch(error => notice(error.message));
    return item;
}

function moreActions(...items) {
    const menu = document.createElement('details');
    menu.className = 'moreActions';
    const summary = document.createElement('summary');
    summary.textContent = '更多';
    const content = document.createElement('div');
    content.className = 'moreActionsContent';
    content.append(...items);
    menu.append(summary, content);
    menu.addEventListener('click', event => {
        if (event.target.closest('button,a')) menu.open = false;
    });
    menu.addEventListener('keydown', event => {
        if (event.key === 'Escape') { menu.open = false; summary.focus(); }
    });
    return menu;
}

document.addEventListener('click', event => {
    for (const menu of document.querySelectorAll('details.moreActions[open]')) {
        if (!menu.contains(event.target) || event.target.closest('button,a')) menu.open = false;
    }
});
document.addEventListener('keydown', event => {
    if (event.key !== 'Escape') return;
    for (const menu of document.querySelectorAll('details.moreActions[open]')) {
        const focused = menu.contains(document.activeElement);
        menu.open = false;
        if (focused) menu.querySelector('summary').focus();
    }
});

function downloadLink(file, label = '下载 EPUB') {
    const link = document.createElement('a');
    link.className = 'button';
    link.textContent = label;
    link.href = 'api/download?file=' + encodeURIComponent(file);
    return link;
}

function tab(page) {
    if (page !== 'terms' && $('reviewPanel')) $('reviewPanel').hidden = true;
    for (const name of ['translate', 'books', 'terms']) {
        const selected = name === page;
        $(name + 'Page').hidden = !selected;
        $(name + 'Tab').classList.toggle('active', selected);
        $(name + 'Tab').setAttribute('aria-pressed', String(selected));
    }
    if (page === 'terms') loadTerms().catch(error => notice(error.message));
}
$('translateTab').onclick = () => tab('translate');
$('booksTab').onclick = () => tab('books');
$('termsTab').onclick = () => tab('terms');

async function read(project) {
    const popup = window.open('about:blank', '_blank');
    try {
        const result = await api('read', {project});
        if (popup) popup.location = result.url;
        else notice('浏览器阻止了阅读窗口，请允许弹出窗口后重试');
    } catch (error) {
        if (popup) popup.close();
        throw error;
    }
}

function renderRecent() {
    const fingerprint = JSON.stringify(state.history);
    if (fingerprint !== recentFingerprint && document.activeElement !== $('recent')) {
        const previous = $('recent').value;
        const placeholder = new Option(state.history.length ? '选择小说，或直接输入新网址' : '暂无记录，可直接输入小说网址', '');
        $('recent').replaceChildren(placeholder);
        for (const item of state.history) {
            $('recent').append(new Option(item.title, item.url || item.source));
        }
        $('recent').value = previous;
        recentFingerprint = fingerprint;
    }
    $('recent').disabled = !!state.restart_pending;
}
$('recent').onchange = () => {
    const item = state?.history.find(item => (item.url || item.source) === $('recent').value);
    if (!item) return;
    $('url').value = item.url || '';
    $('source').value = item.url ? '' : item.source || '';
    $('language').value = item.language || 'zh-Hans';
    if (!item.url) $('settings').open = true;
    notice('');
};
$('url').oninput = () => { $('recent').value = ''; };

function renderBooks() {
    const fingerprint = JSON.stringify(state.projects);
    if (fingerprint !== projectsFingerprint && document.activeElement !== $('project')) {
        const previous = $('project').value;
        $('project').replaceChildren();
        for (const project of state.projects) {
            $('project').append(new Option(`${project.title} · ${project.language || '译文'} · ${project.term_count || 0} 条术语`, project.id));
        }
        if (state.projects.some(project => project.id === previous)) $('project').value = previous;
        projectsFingerprint = fingerprint;
    }
    const search = $('bookSearch').value.trim().toLocaleLowerCase();
    const filter = $('bookFilter').value;
    const matches = state.projects.filter(project =>
        project.title.toLocaleLowerCase().includes(search) &&
        (filter === 'all' || (filter === 'completed' ? project.completed : !project.completed)));
    const pageCount = Math.max(1, Math.ceil(matches.length / booksPageSize));
    booksPage = Math.max(1, Math.min(booksPage, pageCount));
    const listFingerprint = JSON.stringify([state.projects, search, filter, booksPage]);
    if (listFingerprint !== booksFingerprint) {
        $('books').replaceChildren();
        for (const project of matches.slice((booksPage - 1) * booksPageSize, booksPage * booksPageSize)) {
            const row = document.createElement('div');
            row.className = 'book';
            const info = document.createElement('div'), name = document.createElement('strong'), description = document.createElement('p');
            name.textContent = project.title;
            description.textContent = project.kind === 'source' ?
                `原文已获取 ${project.acquired_chapters}/${project.total_chapters} 章 · ${project.completed ? '获取完成，尚未翻译' : '获取未完成'}` :
                `已译 ${project.chapters}/${project.total_chapters || project.chapters} 章 · ${project.language || '译文'} · ${project.completed ? '翻译完成' : '翻译未完成'}` +
                (project.acquired_chapters ? ` · 原文已获取 ${project.acquired_chapters}/${project.total_chapters} 章` : '');
            info.append(name, description);
            const actions = document.createElement('div');
            actions.className = 'actions';
            const secondaryActions = [];
            if (project.kind !== 'source') actions.append(button('阅读', () => read(project.id)));
            if (project.resume) {
                const resume = button(project.kind === 'source' ? '开始翻译' : '继续翻译', async () => {
                    if (!$('model').value) throw Error('请先选择已安装模型');
                    const result = await api('resume', {project: project.id, model: $('model').value,
                        language: project.kind === 'source' ? $('language').value : undefined});
                    tab('translate');
                    notice(result.queued ? '已加入翻译队列' : '已开始接续翻译');
                });
                resume.className = 'resume';
                actions.append(resume);
            }
            if (project.kind === 'source' && !project.completed) {
                const acquire = button('继续获取', async () => {
                    await api('enqueue', {slot: 'acquisition', task: {url: project.acquire_url, title: project.title}});
                    tab('translate'); await poll();
                });
                acquire.className = 'acquireAnother'; actions.append(acquire);
            }
            if (project.source_download) secondaryActions.push(downloadLink(project.source_download, '下载原文'));
            if (project.download) secondaryActions.push(downloadLink(project.download));
            if (project.update_url && project.source_download) {
                for (const [translate, label] of [[true, '检查更新并翻译'], [false, '仅更新原文']]) {
                    const check = button(label, async () => {
                        await api('check-book-updates', {project: project.id, translate, model: $('model').value,
                            language: project.kind === 'source' ? $('language').value : project.language});
                        tab('translate'); notice('已加入更新检查队列'); await poll();
                    });
                    check.className = translate ? 'checkUpdates' : 'checkSourceUpdates'; secondaryActions.push(check);
                }
            }
            if (secondaryActions.length) actions.append(moreActions(...secondaryActions));
            row.append(info, actions);
            $('books').append(row);
        }
        if (!matches.length) $('books').textContent = state.projects.length ? '没有符合条件的作品。' : '暂无作品，开始翻译后会显示在这里。';
        booksFingerprint = listFingerprint;
    }
    $('booksPagination').textContent = `第 ${booksPage} / ${pageCount} 页 · 每页 ${booksPageSize} 部 · 共 ${matches.length} 部`;
    $('booksPrevious').disabled = booksPage <= 1;
    $('booksNext').disabled = booksPage >= pageCount;
    if (!manualProject && !dirty && state.projects.some(project => project.id === state.task.project)) {
        $('project').value = state.task.project;
    }
    document.querySelectorAll('.resume').forEach(item => { item.disabled = !!state.restart_pending || modelsLoading || !$('model').value; });
    document.querySelectorAll('.checkUpdates').forEach(item => { item.disabled = !!state.restart_pending || modelsLoading || !$('model').value; });
    document.querySelectorAll('.checkSourceUpdates').forEach(item => { item.disabled = !!state.restart_pending; });
}

$('bookSearch').oninput = $('bookFilter').onchange = () => {
    booksPage = 1;
    if (state) renderBooks();
};
$('booksPrevious').onclick = () => { booksPage--; renderBooks(); };
$('booksNext').onclick = () => { booksPage++; renderBooks(); };

function renderStatus() {
    const task = state.task, busy = task.running || task.recovering;
    $('start').textContent = busy ? '加入翻译队列' : '获取并翻译';
    $('start').className = 'primary';
    $('start').disabled = taskActionPending || !!state.restart_pending || modelsLoading || !$('model').value;
    $('model').disabled = modelsLoading || !$('model').options.length;
    $('refreshModels').disabled = modelsLoading;
    $('progress').value = task.percent;
    $('percent').textContent = task.percent + '%';
    $('message').textContent = task.message;
    $('save').disabled = busy;
    $('import').disabled = busy;
    const counts = task.counts;
    $('pipelineCounts').hidden = !counts;
    if (counts) $('pipelineCounts').textContent = `获取 ${counts.acquired}/${counts.total} 章 · 翻译 ${counts.translated}/${counts.total} 章`;
    for (const [key, id, label] of [['acquisition', 'stageAcquisition', '获取'], ['translation', 'stageTranslation', '翻译'], ['glossary', 'stageGlossary', '术语']]) {
        $(id).textContent = `${label}：${task.stages?.[key] || '准备就绪'}`;
    }
    const fingerprint = JSON.stringify([task.output, task.project]);
    if (fingerprint !== resultFingerprint) {
        $('result').replaceChildren();
        if (task.output && task.project) {
            const filename = task.output.replaceAll('\\', '/').split('/').at(-1);
            $('result').append(downloadLink(task.project + '/' + filename),
                button('阅读译文', () => read(task.project)), button('查看小说书库', () => tab('books')));
        }
        resultFingerprint = fingerprint;
    }
}

async function poll() {
    if (poll.inFlight) { poll.again = true; return; }
    poll.inFlight = true;
    try {
        state = await api('status');
        if ($('notice').textContent.startsWith('连接失败：')) notice('');
        renderStatus();
        renderBooks();
        renderRecent();
        renderAcquisition();
        if (typeof renderTaskTools === 'function') renderTaskTools();
        if (typeof renderReviewState === 'function') renderReviewState();
        if (!$('termsPage').hidden && !dirty) await loadTerms();
    } catch (error) { notice('连接失败：' + error.message); }
    finally {
        poll.inFlight = false;
        clearTimeout(pollTimer);
        const hidden = typeof document !== 'undefined' && document.hidden;
        pollTimer = setTimeout(poll, poll.again ? 0 : hidden ? 30000 : 2500);
        poll.again = false;
    }
}

function renderAcquisition() {
    const acquisition = state.acquisition_task || {};
    const busy = acquisition.running || acquisition.recovering;
    $('acquireOnly').textContent = busy ? '加入获取队列' : '只获取原文';
    $('acquireOnly').className = 'secondary';
    $('acquireOnly').disabled = acquisitionActionPending || !!state.restart_pending;
    $('acquisitionMessage').textContent = busy || acquisition.url || acquisition.message?.startsWith('自动恢复失败') ?
        `独立获取：${acquisition.percent || 0}% · ${acquisition.message}` : '只获取原文，不调用翻译模型；当前翻译可继续进行。';
    document.querySelectorAll('.acquireAnother').forEach(button => { button.disabled = !!state.restart_pending; });
}
$('acquireOnly').onclick = async () => {
    if (acquisitionActionPending) return;
    acquisitionActionPending = true; $('acquireOnly').disabled = true;
    try {
        const result = await api('enqueue', {slot: 'acquisition', task: {url: $('url').value}});
        showQueueSubmission(result);
        await poll();
    }
    catch (error) { notice(error.message); }
    finally { acquisitionActionPending = false; if (state) renderAcquisition(); }
};

function filterTerms() {
    const query = $('termSearch').value.trim().toLocaleLowerCase();
    const matches = entries.map((entry, index) => ({entry, index})).filter(({entry}) =>
        [entry.source, entry.target, entry.category].some(value => String(value || '').toLocaleLowerCase().includes(query)));
    const pageCount = Math.max(1, Math.ceil(matches.length / termsPageSize));
    termsPage = Math.min(termsPage, pageCount);
    $('termRows').replaceChildren();
    for (const {entry, index} of matches.slice((termsPage - 1) * termsPageSize, termsPage * termsPageSize)) {
        const row = document.createElement('tr');
        row.dataset.index = index;
        for (const key of ['source', 'target', 'category']) {
            const cell = document.createElement('td');
            if (key === 'target') {
                const input = document.createElement('input');
                input.value = entry.target;
                input.setAttribute('aria-label', entry.source + '的译名');
                input.oninput = () => { entry.target = input.value; termEdits.set(entry.source, entry); dirty = true; };
                cell.append(input);
            } else cell.textContent = entry[key];
            row.append(cell);
        }
        const examples = document.createElement('td');
        examples.append(button('例句', () => showExamples(entry.source)));
        row.append(examples);
        $('termRows').append(row);
    }
    $('termCount').textContent = query ? `找到 ${matches.length} / ${entries.length} 条术语` : `共 ${entries.length} 条术语`;
    $('termsPagination').textContent = `第 ${termsPage} / ${pageCount} 页 · 每页 ${termsPageSize} 条`;
    $('termsPrevious').disabled = termsPage <= 1;
    $('termsNext').disabled = termsPage >= pageCount;
    $('emptyTerms').hidden = !!matches.length;
    $('emptyTerms').textContent = entries.length ? '没有符合搜索条件的术语。' : '此作品暂无术语，完成章节后会自动提取并保存。';
}
$('termSearch').oninput = () => { termsPage = 1; filterTerms(); };
$('termsPrevious').onclick = () => { termsPage--; filterTerms(); };
$('termsNext').onclick = () => { termsPage++; filterTerms(); };

async function showExamples(source) {
    const project = loadedProject;
    const request = ++examplesRequest;
    $('exampleTitle').textContent = source + ' · 原文／译文例句';
    $('exampleContent').replaceChildren();
    $('exampleContent').textContent = '正在查找已保存的例句…';
    if (!$('examples').open) $('examples').showModal();
    try {
        const result = await api('examples?project=' + encodeURIComponent(project) + '&source=' + encodeURIComponent(source));
        if (request !== examplesRequest || project !== loadedProject || !$('examples').open) return;
        $('exampleContent').replaceChildren();
        for (const example of result.examples) {
            const entry = document.createElement('article');
            const title = document.createElement('strong'), original = document.createElement('p'), translation = document.createElement('p');
            title.textContent = example.chapter;
            original.textContent = '原文：' + example.original;
            translation.textContent = '译文：' + example.translation;
            entry.append(title, original, translation);
            $('exampleContent').append(entry);
        }
        if (!result.examples.length) $('exampleContent').textContent = result.reason === 'no_originals'
            ? '此作品尚未保存可查询的原文，请在继续翻译时使用原始 EPUB 或原网址。'
            : '已保存章节中未找到此原词的例句，请核对原词或等待相关章节保存。';
    } catch (error) {
        if (request !== examplesRequest || project !== loadedProject || !$('examples').open) return;
        $('exampleContent').textContent = '查找例句失败：' + error.message;
    }
}
$('closeExamples').onclick = () => { examplesRequest++; $('examples').close(); };
$('examples').oncancel = () => { examplesRequest++; };

async function loadTerms() {
    if (termsLoading) return;
    const project = $('project').value;
    if (dirty && project === loadedProject) return;
    if (dirty && project !== loadedProject && !confirm('放弃未保存的术语修改？')) { $('project').value = loadedProject; return; }
    const sameProject = project === loadedProject;
    termsLoading = true;
    try {
        const data = project ? await api('terms?project=' + encodeURIComponent(project) + '&revision=' + encodeURIComponent(sameProject ? termsRevision : '')) : {entries: [], revision: ''};
        if (project !== $('project').value || (dirty && sameProject)) return;
        if (data.unchanged && sameProject) return;
        entries = data.entries;
        loadedProject = project;
        termsRevision = data.revision;
        dirty = false;
        termEdits.clear();
        if (!sameProject) { termsPage = 1; examplesRequest++; $('examples').close(); }
        filterTerms();
    } finally { termsLoading = false; }
}
$('project').onchange = () => { manualProject = true; loadTerms().catch(error => notice(error.message)); };

$('startForm').onsubmit = async event => {
    event.preventDefault();
    if (taskActionPending) return;
    taskActionPending = true; notice(''); $('start').disabled = true;
    try {
        const result = await api('enqueue', {slot: 'translation', task: {url: $('url').value, source: $('source').value, language: $('language').value, model: $('model').value}});
        showQueueSubmission(result);
        await poll();
    }
    catch (error) { notice(error.message); }
    finally { taskActionPending = false; if (state) renderStatus(); }
};
$('save').onclick = async () => {
    try {
        const result = await api('terms', {project: loadedProject, entries: [...termEdits.values()], merge: true});
        dirty = false; termsRevision = '';
        await loadTerms(); notice('已保存 ' + result.saved + ' 条术语');
    } catch (error) { notice(error.message); }
};
$('export').onclick = () => {
    if (!loadedProject) return notice('请先选择作品');
    const url = URL.createObjectURL(new Blob([JSON.stringify({entries}, null, 2)], {type: 'application/json'}));
    const link = document.createElement('a'); link.href = url; link.download = 'glossary.json'; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
};
$('import').onclick = () => { if (!loadedProject) notice('请先选择作品'); else $('importFile').click(); };
$('importFile').onchange = async () => {
    const file = $('importFile').files[0]; if (!file) return;
    try {
        if (dirty) throw Error('请先保存当前修改，再导入文件');
        const data = JSON.parse(await file.text());
        let incoming = Array.isArray(data) ? data : data.entries;
        if (!incoming && typeof data === 'object' && data !== null) incoming = Object.entries(data).map(([source, target]) => ({source, target}));
        if (!Array.isArray(incoming) || incoming.some(entry => !entry || typeof entry.source !== 'string' || typeof entry.target !== 'string' || !entry.source.trim() || !entry.target.trim())) throw Error('每条术语都需要非空的 source 和 target');
        const result = await api('terms', {project: loadedProject, entries: incoming, merge: true});
        termsRevision = ''; await loadTerms(); notice('已导入并保存，当前共 ' + result.saved + ' 条术语');
    } catch (error) { notice(error.message); }
    finally { $('importFile').value = ''; }
};

async function refreshModels() {
    const previous = $('model').value;
    modelsLoading = true; $('model').disabled = true; $('refreshModels').disabled = true; $('start').disabled = true;
    $('modelStatus').textContent = '正在连接 Ollama…';
    try {
        const data = await api('models'); $('model').replaceChildren();
        for (const name of data.models) $('model').append(new Option(name, name));
        $('model').value = data.models.includes(previous) ? previous : data.selected;
        $('modelStatus').textContent = data.models.length ? '' : '没有已安装模型，请安装后刷新';
    } catch (error) { $('model').replaceChildren(); $('modelStatus').textContent = error.message; }
    finally { modelsLoading = false; if (state) { renderStatus(); renderBooks(); } }
}
$('refreshModels').onclick = refreshModels;
document.addEventListener('visibilitychange', () => { if (!document.hidden) poll(); });
window.addEventListener('focus', () => poll());
refreshModels(); poll();
