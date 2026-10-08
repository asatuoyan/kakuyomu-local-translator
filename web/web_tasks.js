// Queue, recovery and backup controls share the existing status response.
const queuePanel = document.createElement('section');
queuePanel.className = 'card';
const queueTitle = document.createElement('h2'); queueTitle.textContent = '任务队列';
const queueList = document.createElement('div');
const activeList = document.createElement('div');
const completedList = document.createElement('details');
const completedSummary = document.createElement('summary'); completedSummary.textContent = '最近完成';
const completedRows = document.createElement('div'); completedList.append(completedSummary, completedRows);
const queueStatus = document.createElement('p'); queueStatus.className = 'muted';
const idleCloseStatus = document.createElement('p'); idleCloseStatus.className = 'muted';
const idleCloseToggle = button('取消自动关闭', async () => {
    await api('idle-close', {enabled: !state.idle_close?.enabled}); await poll();
});
let lastIdleActivity = -Infinity;
function recordIdleActivity(event) {
    if (!event.isTrusted || Date.now() - lastIdleActivity < 1000) return;
    lastIdleActivity = Date.now();
    api('activity', {}).catch(() => {});
}
for (const event of ['pointerdown', 'keydown', 'input', 'wheel', 'scroll']) {
    document.addEventListener(event, recordIdleActivity, {passive: true, capture: true});
}
const pauseQueue = button('暂停全部', async () => { await api('queue-control', {paused: !state.queue_paused}); await poll(); });
const diagnosticDownload = button('导出诊断信息', async () => {
    const data = await api('diagnostics', {});
    const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], {type: 'application/json'}));
    const link = document.createElement('a'); link.href = url; link.download = 'translator-diagnostics.json'; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
});
const backupEntry = button('备份与恢复', () => { tab('terms'); backupsPanel.open = true; backupsPanel.scrollIntoView({block: 'start'}); });
const queueToolbar = document.createElement('div'); queueToolbar.className = 'actions';
queueToolbar.append(pauseQueue, idleCloseToggle, moreActions(backupEntry, diagnosticDownload));
queuePanel.id = 'taskQueue';
queuePanel.append(queueTitle, queueToolbar, queueStatus, idleCloseStatus, activeList, queueList, completedList); $('translatePage').append(queuePanel);
function showQueueSubmission(result) {
    notice(result.duplicate ? '该小说已有任务，已定位；可在队列中继续或重试' : state?.queue_paused ? '已加入队列，等待继续队列' : '已加入队列，空闲时自动开始');
    tab('translate');
    setTimeout(() => {
        const row = result.id ? document.getElementById('queue-' + result.id) : document.getElementById('active-' + result.slot);
        (row || queuePanel).scrollIntoView({block: 'nearest', behavior: 'smooth'});
        if (row) { row.tabIndex = -1; row.focus({preventScroll: true}); }
    }, 0);
}
const recoveryControls = new Map();
for (const [slot, parent] of [['translation', $('message')], ['acquisition', $('acquisitionMessage')]]) {
    const container = document.createElement('div'); container.className = 'actions';
    const reason = document.createElement('span');
    const retry = button('重试恢复', async () => { await api('retry-recovery', {slot}); await poll(); });
    const cancel = button('取消恢复', async () => { await api('cancel-recovery', {slot}); await poll(); });
    const edit = button('修改来源或模型', () => openRecoveryEdit(slot));
    container.append(reason, retry, moreActions(edit, cancel)); parent.after(container); recoveryControls.set(slot, {container, reason, retry, cancel, edit});
}
let queueFingerprint = '';
function renderTaskTools() {
    const idle = state.idle_close;
    idleCloseToggle.textContent = idle?.enabled ? '取消自动关闭' : '启用自动关闭';
    idleCloseToggle.disabled = !!state.restart_pending;
    idleCloseStatus.textContent = !idle?.enabled ? '自动关闭已取消。' : idle.remaining_seconds == null ?
        '队列全部完成后，无操作一小时自动关闭翻译程序；操作会重新计时。' :
        `队列已完成，${Math.floor(idle.remaining_seconds / 60)} 分 ${idle.remaining_seconds % 60} 秒后自动关闭翻译程序；操作会重新计时。`;
    pauseQueue.textContent = state.queue_paused ? '继续队列' : '暂停全部';
    pauseQueue.disabled = !!state.restart_pending;
    activeList.replaceChildren();
    for (const [slot, task] of [['translation', state.task], ['acquisition', state.acquisition_task || {}]]) {
        if (!task.running && !task.recovering) continue;
        const row = document.createElement('div'); row.className = 'book'; row.id = 'active-' + slot;
        const label = document.createElement('span'); label.textContent = `${task.title || '当前小说'} · ${slot === 'translation' ? '翻译' : '获取'} · ${task.message}`;
        const pause = button(task.stopping ? '正在暂停…' : task.kind === 'review' ? '停止重译' : '暂停本书', async () => {
            if (pause.disabled) return;
            pause.disabled = true;
            try { await api('pause-task', {slot, job_id: task.kind === 'review' ? undefined : task.job_id}); }
            finally { await poll(); }
        });
        pause.disabled = !!task.stopping || !!state.restart_pending;
        row.append(label, pause); activeList.append(row);
    }
    completedRows.replaceChildren();
    for (const task of (state.completed_tasks || []).slice(-5).reverse()) {
        const row = document.createElement('p'); row.textContent = `${task.title} · ${task.slot === 'translation' ? '翻译完成' : '获取完成'}`; completedRows.append(row);
    }
    completedList.hidden = !(state.completed_tasks || []).length;
    queueStatus.textContent = state.queue_paused ? '已暂停：当前任务保存进度后停止，后续任务等待继续。重启后仍保持暂停。' : '获取与翻译分别排队；继续时优先恢复未完成任务。';
    if (state.queue_paused && state.task.kind === 'review') queueStatus.textContent += ' 局部重译需重新检查后手动启动。';
    for (const [slot, controls] of recoveryControls) {
        const task = slot === 'translation' ? state.task : state.acquisition_task || {};
        controls.container.hidden = !task.retry_available || (state.queue || []).some(item => item.id === task.retry_body?._queue_id);
        controls.reason.textContent = task.recovery_error?.advice || '';
        controls.retry.disabled = !!task.running || !!task.recovering || !!state.restart_pending;
        controls.edit.disabled = !!task.running || !!task.recovering || !!state.restart_pending;
        controls.cancel.disabled = !!task.running || !!state.restart_pending;
    }
    const busy = !!state.restart_pending || !!state.task.running || !!state.task.recovering || !!state.acquisition_task?.running || !!state.acquisition_task?.recovering;
    if (typeof createGroupControl !== 'undefined') {
        createGroupControl.disabled = busy || !backupProjects.value;
        restoreGroupControl.disabled = busy || !groupRows.length;
    }
    const fingerprint = JSON.stringify([state.queue, state.queue_error, state.restart_pending, state.queue_paused]);
    if (fingerprint === queueFingerprint) return;
    queueFingerprint = fingerprint; queueList.replaceChildren();
    if (state.queue_error) { const error = document.createElement('p'); error.textContent = '队列记录无法读取：' + state.queue_error; queueList.append(error); }
    const counts = {translation: 0, acquisition: 0};
    const ordered = ['waiting', 'paused', 'failed'].flatMap(status => (state.queue || []).filter(item => item.status === status));
    let sectionStatus = '';
    for (const item of ordered) {
        if (item.status !== sectionStatus) {
            const heading = document.createElement('h3');
            heading.textContent = {waiting: '等待执行', paused: '已暂停', failed: '需要处理'}[item.status];
            queueList.append(heading); sectionStatus = item.status;
        }
        const row = document.createElement('div'); row.className = 'book'; row.id = 'queue-' + item.id;
        const description = document.createElement('span');
        const order = item.status === 'failed' ? '需要处理' : item.status === 'paused' ? '已暂停' : '等待第 ' + (++counts[item.slot]) + ' 项';
        description.textContent = `${item.slot === 'translation' ? '翻译' : '获取'} · ${order} · ${item.title}` +
            (item.error ? ` · ${item.error.advice} ${item.error.detail}` : '');
        const actions = document.createElement('div'); actions.className = 'actions';
        const secondaryActions = [];
        for (const [action, label] of [['retry', '重试'], ['resume', '继续本书'], ['cancel', '取消排队'], ['priority', '优先处理'], ['pause', '暂停本书']]) {
            if (action === 'retry' && item.status !== 'failed') continue;
            if (action === 'resume' && item.status !== 'paused') continue;
            if (['priority', 'pause'].includes(action) && item.status !== 'waiting') continue;
            const control = button(label, async () => { await api('queue-action', {id: item.id, action}); await poll(); });
            control.disabled = !!state.restart_pending;
            if (['priority', 'pause'].includes(action) || item.status !== 'waiting' && action === 'cancel') secondaryActions.push(control);
            else actions.append(control);
        }
        if (item.status === 'failed') secondaryActions.unshift(button('修改后重试', () => openRecoveryEdit(item.slot, item.id)));
        if (secondaryActions.length) actions.append(moreActions(...secondaryActions));
        row.append(description, actions); queueList.append(row);
    }
    if (!(state.queue || []).length && !state.queue_error) queueList.textContent = '暂无排队任务。获取和翻译分别排队，翻译一次处理一本。';
}

let recoveryEditTarget = null, recoveryEditGeneration = 0;
const recoveryDialog = document.createElement('dialog');
const recoveryHeading = document.createElement('h2'); recoveryHeading.textContent = '调整失败任务';
const recoverySource = document.createElement('input'); recoverySource.placeholder = '原文 EPUB 完整路径（留空保留原来源）';
recoverySource.setAttribute('aria-label', '重新选择原文 EPUB');
const recoveryModel = document.createElement('select'); recoveryModel.setAttribute('aria-label', '更换翻译模型');
const chooseRecoverySource = button('选择原文文件', async () => {
    const generation = recoveryEditGeneration;
    chooseRecoverySource.disabled = true;
    try {
        const result = await api('choose-source', {});
        if (generation === recoveryEditGeneration && recoveryDialog.open && result.source) recoverySource.value = result.source;
    } finally { chooseRecoverySource.disabled = false; }
});
const retryEditedTask = button('按新设置重试', async () => {
    if (!recoveryEditTarget) return;
    const {slot, id} = recoveryEditTarget;
    const settings = slot === 'translation' ? {model: recoveryModel.value} : {};
    if (slot === 'translation' && recoverySource.value.trim()) settings.source = recoverySource.value.trim();
    await api(id ? 'queue-action' : 'retry-recovery', id ? {id, action: 'retry', settings} : {slot, settings});
    recoveryDialog.close(); await poll();
});
const closeRecoveryEdit = button('关闭', () => recoveryDialog.close());
recoveryDialog.append(recoveryHeading, recoverySource, chooseRecoverySource, recoveryModel, retryEditedTask, closeRecoveryEdit);
document.body.append(recoveryDialog);
recoveryDialog.addEventListener('close', () => { recoveryEditTarget = null; recoveryEditGeneration++; });
function openRecoveryEdit(slot, id = null) {
    const task = slot === 'translation' ? state.task : state.acquisition_task || {};
    const body = id ? state.queue.find(item => item.id === id)?.body : task.retry_body || task;
    if (!body) return;
    recoveryEditTarget = {slot, id}; recoveryEditGeneration++;
    recoverySource.value = body.source || '';
    recoveryModel.replaceChildren();
    for (const option of $('model').options) recoveryModel.append(new Option(option.textContent, option.value));
    const model = body.model || state.model;
    if (model && ![...recoveryModel.options].some(option => option.value === model)) recoveryModel.append(new Option(model + '（原模型）', model));
    recoveryModel.value = model;
    recoverySource.hidden = chooseRecoverySource.hidden = recoveryModel.hidden = slot !== 'translation';
    if (!recoveryDialog.open) recoveryDialog.showModal();
}

const backupsPanel = document.createElement('details');
const backupSummary = document.createElement('summary'); backupSummary.textContent = '恢复术语与进度备份';
const backupProjects = document.createElement('select'); backupProjects.setAttribute('aria-label', '备份作品目录');
const backupFiles = document.createElement('select'); backupFiles.setAttribute('aria-label', '选择备份');
let backupRows = [];
let groupRows = [];
const groupFiles = document.createElement('select'); groupFiles.setAttribute('aria-label', '选择整组备份');
async function loadBackupFiles() {
    backupFiles.replaceChildren();
    groupFiles.replaceChildren();
    const result = backupProjects.value ? await api('backups', {project: backupProjects.value}) : {};
    backupRows = result.backups || []; groupRows = result.groups || [];
    backupRows.forEach((row, i) => backupFiles.append(new Option(`${row.file} · ${new Date(row.saved_at * 1000).toLocaleString()}`, String(i))));
    restoreControl.disabled = !backupRows.length;
    groupRows.forEach((row, i) => groupFiles.append(new Option(`${new Date(row.saved_at * 1000).toLocaleString()} · ${row.chapters} 章 · ${row.files} 个文件`, String(i))));
    createGroupControl.disabled = !backupProjects.value;
    restoreGroupControl.disabled = !groupRows.length;
}
backupProjects.onchange = () => loadBackupFiles().catch(error => notice(error.message));
const refreshBackups = button('加载备份', async () => {
    const result = await api('backups', {}); backupProjects.replaceChildren();
    for (const folder of result.projects) backupProjects.append(new Option(folder, folder));
    await loadBackupFiles();
});
const restoreControl = button('恢复所选备份', async () => {
    const row = backupRows[Number(backupFiles.value)];
    if (!row) return;
    if (dirty) throw Error('请先保存或重新载入未保存的术语修改');
    if (!window.confirm('恢复将替换所选文件。当前有效文件会先保留为备份，确定恢复吗？')) return;
    await api('restore-backup', {project: backupProjects.value, file: row.file, id: row.id});
    loadedProject = ''; termsRevision = ''; notice('备份已恢复'); await poll(); await loadBackupFiles();
});
restoreControl.disabled = true;
const createGroupControl = button('创建整组备份', async () => {
    if (dirty) throw Error('请先保存术语修改');
    await api('create-group-backup', {project: backupProjects.value}); notice('整组备份已创建'); await loadBackupFiles();
});
const restoreGroupControl = button('恢复整组备份', async () => {
    const row = groupRows[Number(groupFiles.value)]; if (!row) return;
    if (dirty) throw Error('请先保存或重新载入术语修改');
    if (!window.confirm('将一起恢复该时点的项目进度、章节和术语。现有记录会保留，导出的 EPUB 需续译后重新生成。确定恢复吗？')) return;
    await api('restore-group-backup', {project: backupProjects.value, id: row.id});
    loadedProject = ''; termsRevision = ''; notice('整组备份已恢复'); await poll(); await loadBackupFiles();
});
createGroupControl.disabled = restoreGroupControl.disabled = true;
backupsPanel.append(backupSummary, backupProjects, refreshBackups, groupFiles, createGroupControl, restoreGroupControl, backupFiles, restoreControl);
$('termsPage').append(backupsPanel);
