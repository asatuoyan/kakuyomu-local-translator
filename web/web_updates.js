const loadedFrontendRevision = document.querySelector('meta[name="frontend-revision"]').content;
const loadedServerInstance = document.querySelector('meta[name="server-instance"]').content;
let updateState = null, updateActionPending = false, updateReloading = false, updateTimer = null;

function refreshUpdatedPage() {
    if (dirty) return notice('请先保存术语修改，再更新页面');
    if (updateReloading) return;
    updateReloading = true;
    try {
        const values = {};
        for (const id of ['url', 'source', 'language', 'model', 'bookSearch', 'bookFilter', 'project']) values[id] = $(id).value;
        const page = ['translate', 'books', 'terms'].find(name => !$(name + 'Page').hidden) || 'translate';
        sessionStorage.setItem('web-update-state:' + token, JSON.stringify({values, page}));
    } catch {}
    location.reload();
}

function renderUpdates() {
    if (!updateState) return;
    const changedPage = updateState.frontend_revision !== loadedFrontendRevision;
    const restarted = updateState.instance !== loadedServerInstance;
    const backend = updateState.backend_changed, pending = updateState.restart_pending;
    $('updatesPanel').hidden = !(changedPage || backend || pending || restarted || updateState.error);
    $('updateError').textContent = updateState.error || '';
    $('updateMessage').textContent = pending ? '已安排更新重启，等待任务保存并结束；重启后页面重新连接，未完成的获取与翻译会自动恢复。' :
        restarted ? '服务已重启。请先保存未保存的术语，再重新整理页面。' :
        backend ? '检测到后端更新，重启服务后生效。已保存作品和续译缓存会保留；阅读窗口需重新打开。' :
        '检测到页面更新，重新整理即可生效，下载和翻译继续进行。';
    $('updateRefresh').hidden = !(changedPage || restarted) || pending;
    $('updateRefresh').disabled = updateActionPending || dirty;
    const restartAvailable = backend && updateState.restart_supported && !pending;
    $('updateRestart').hidden = !restartAvailable || updateState.busy;
    $('updateWait').hidden = $('updateStop').hidden = !restartAvailable || !updateState.busy;
    $('updateCancel').hidden = !pending;
    for (const id of ['updateRestart', 'updateWait', 'updateStop', 'updateCancel']) $(id).disabled = updateActionPending || dirty;
    if (backend && !updateState.restart_supported) $('updateMessage').textContent += ' 当前启动方式请手动重启 web_app.py。';
    if (dirty && (changedPage || backend || restarted)) $('updateMessage').textContent += ' 请先保存术语修改。';
}

async function checkUpdates() {
    if (checkUpdates.inFlight) { checkUpdates.again = true; return; }
    checkUpdates.inFlight = true;
    try {
        updateState = await api('updates');
        if (updateState.instance !== loadedServerInstance && !dirty) { refreshUpdatedPage(); return; }
        renderUpdates();
    } catch {
        if (updateState?.restart_pending) {
            $('updatesPanel').hidden = false;
            $('updateMessage').textContent = '服务正在重启，等待重新连接…';
        }
    } finally {
        checkUpdates.inFlight = false;
        clearTimeout(updateTimer);
        const hidden = typeof document !== 'undefined' && document.hidden;
        if (!updateReloading) updateTimer = setTimeout(checkUpdates, checkUpdates.again ? 0 : hidden ? 30000 : 3000);
        checkUpdates.again = false;
    }
}

async function restartForUpdate(mode) {
    if (dirty) return notice('请先保存术语修改，再安排更新重启');
    if (updateActionPending) return;
    updateActionPending = true; renderUpdates();
    try {
        await api('restart', {mode});
        await checkUpdates();
    } catch (error) { notice(error.message); }
    finally { updateActionPending = false; renderUpdates(); }
}
$('updateRefresh').onclick = refreshUpdatedPage;
$('updateRestart').onclick = () => restartForUpdate('now');
$('updateWait').onclick = () => restartForUpdate('wait');
$('updateStop').onclick = () => restartForUpdate('stop');
$('updateCancel').onclick = async () => {
    if (updateActionPending) return;
    updateActionPending = true; renderUpdates();
    try { await api('cancel-restart', {}); await checkUpdates(); }
    catch (error) { notice(error.message); }
    finally { updateActionPending = false; renderUpdates(); }
};

function restoreUpdateState() {
    let saved;
    try { saved = JSON.parse(sessionStorage.getItem('web-update-state:' + token)); } catch { return; }
    if (!saved) return;
    if (!state || modelsLoading) { setTimeout(restoreUpdateState, 100); return; }
    try { sessionStorage.removeItem('web-update-state:' + token); } catch {}
    for (const [id, value] of Object.entries(saved.values || {})) if ($(id)) $(id).value = value;
    manualProject = !!$('project').value;
    if (['translate', 'books', 'terms'].includes(saved.page)) tab(saved.page);
    renderBooks(); renderStatus();
}
restoreUpdateState();
document.addEventListener('visibilitychange', () => { if (!document.hidden) checkUpdates(); });
checkUpdates();
