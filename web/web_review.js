let reviewProject = '', reviewData = null, reviewPage = 1, reviewLoading = false;
let reviewRequest = 0, reviewRunning = false;
let reviewActionPending = false;
const reviewSelected = new Set(), reviewPageSize = 50;

async function openReview(project, mode = 'all') {
    if (dirty) return notice('请先保存术语修改，再检查译文');
    if (state?.task.running || state?.task.recovering) return notice('请先停止当前任务再检查译文');
    reviewProject = project;
    reviewData = null; reviewPage = 1; reviewSelected.clear();
    $('reviewMode').value = mode;
    const title = state?.projects.find(item => item.id === project)?.title || project;
    $('reviewTitle').textContent = title + ' · 译文检查';
    tab('terms'); $('reviewPanel').hidden = false;
    $('reviewPanel').scrollIntoView({behavior: 'smooth', block: 'start'});
    await scanReview();
}

async function scanReview() {
    if (!reviewProject || state?.task.running || state?.task.recovering) return;
    const request = ++reviewRequest, project = reviewProject;
    reviewLoading = true; reviewData = null; reviewSelected.clear(); reviewPage = 1;
    $('reviewRows').replaceChildren(); $('reviewUnavailable').replaceChildren();
    $('reviewSummary').textContent = '正在检查已保存章节…'; renderReviewState();
    try {
        const data = await api('review', {project, mode: $('reviewMode').value});
        if (request !== reviewRequest || project !== reviewProject) return;
        reviewData = data; renderReview();
    } catch (error) {
        if (request === reviewRequest) $('reviewSummary').textContent = '检查失败：' + error.message;
    } finally {
        if (request === reviewRequest) { reviewLoading = false; renderReviewState(); }
    }
}

function renderReview() {
    const items = reviewData?.items || [], pages = Math.max(1, Math.ceil(items.length / reviewPageSize));
    reviewPage = Math.min(reviewPage, pages);
    $('reviewRows').replaceChildren();
    for (const item of items.slice((reviewPage - 1) * reviewPageSize, reviewPage * reviewPageSize)) {
        const article = document.createElement('article'), label = document.createElement('label');
        article.style.borderTop = '1px solid #e0e5dd'; article.style.padding = '16px 0';
        const checkbox = document.createElement('input'); checkbox.type = 'checkbox'; checkbox.style.width = 'auto';
        checkbox.checked = reviewSelected.has(item.id); checkbox.dataset.reviewId = item.id;
        checkbox.onchange = () => {
            if (checkbox.checked) reviewSelected.add(item.id); else reviewSelected.delete(item.id);
            renderReviewState();
        };
        label.append(checkbox, document.createTextNode(` ${item.chapter} · 第 ${item.paragraph} 段`)); article.append(label);
        for (const text of [item.reasons.join('；'), '原文：' + item.original, '译文：' + item.translation]) {
            const paragraph = document.createElement('p'); paragraph.textContent = text;
            paragraph.style.whiteSpace = 'pre-wrap'; paragraph.style.overflowWrap = 'anywhere';
            article.append(paragraph);
        }
        $('reviewRows').append(article);
    }
    const missing = reviewData?.unavailable || [];
    $('reviewUnavailable').replaceChildren();
    for (const item of missing) {
        const paragraph = document.createElement('p'); paragraph.textContent = `${item.chapter}：${item.reason}`;
        $('reviewUnavailable').append(paragraph);
    }
    $('reviewSummary').textContent = items.length ? `发现 ${items.length} 个段落待核对` :
        (missing.length ? '可检查章节未发现问题；部分章节缺少原文，尚未完成检查' :
            reviewData?.mode === 'changes' ? '未找到仍使用已记录旧译名的段落；仅检查在本功能启用后修改的译名' : '未发现规则检查问题，请按需人工核对');
    $('reviewPagination').textContent = `第 ${reviewPage} / ${pages} 页 · 每页 ${reviewPageSize} 段`;
    $('reviewPrevious').disabled = reviewPage <= 1; $('reviewNext').disabled = reviewPage >= pages;
    renderReviewState();
}

function renderReviewState() {
    const busy = !!state?.task.running || !!state?.task.recovering;
    const ownTask = busy && state.task.kind === 'review' && state.task.project === reviewProject;
    $('reviewRetranslate').className = ownTask ? 'danger' : 'primary';
    $('reviewScan').disabled = busy || !!state?.restart_pending || reviewLoading;
    $('reviewMode').disabled = busy || !!state?.restart_pending || reviewLoading;
    $('reviewRetranslate').disabled = reviewActionPending || (ownTask ? !!state.task.stopping :
        busy || !!state?.restart_pending || reviewLoading || !reviewSelected.size || !$('model').value);
    $('reviewRetranslate').textContent = ownTask ? (state.task.stopping ? '正在停止重译…' : '停止重译') :
        `重译选中段落（${reviewSelected.size}）`;
    $('reviewSelectPage').disabled = busy || reviewLoading || !reviewData?.items.length;
    $('reviewClear').disabled = busy || reviewLoading || !reviewSelected.size;
    $('reviewPrevious').disabled = reviewLoading || !reviewData || reviewPage <= 1;
    $('reviewNext').disabled = reviewLoading || !reviewData || reviewPage * reviewPageSize >= reviewData.items.length;
    document.querySelectorAll('[data-review-id]').forEach(input => { input.disabled = busy; });
    if (state?.task.kind === 'review' && state.task.project === reviewProject) {
        $('reviewTask').textContent = `${state.task.percent}% · ${state.task.message}`;
        if (reviewRunning && !busy) {
            reviewRunning = false;
            scanReview();
        }
    } else $('reviewTask').textContent = '';
}

$('checkTranslation').onclick = () => {
    if (!loadedProject) return notice('请先选择作品');
    openReview(loadedProject).catch(error => notice(error.message));
};
$('checkOldTerms').onclick = () => {
    if (!loadedProject) return notice('请先选择作品');
    openReview(loadedProject, 'changes').catch(error => notice(error.message));
};
$('reviewScan').onclick = scanReview;
$('reviewMode').onchange = scanReview;
$('reviewClose').onclick = () => { $('reviewPanel').hidden = true; };
$('reviewPrevious').onclick = () => { reviewPage--; renderReview(); };
$('reviewNext').onclick = () => { reviewPage++; renderReview(); };
$('reviewClear').onclick = () => { reviewSelected.clear(); renderReview(); };
$('reviewSelectPage').onclick = () => {
    for (const item of (reviewData?.items || []).slice((reviewPage - 1) * reviewPageSize, reviewPage * reviewPageSize)) reviewSelected.add(item.id);
    renderReview();
};
$('reviewRetranslate').onclick = async () => {
    if (reviewActionPending) return;
    if (state?.task.running && state.task.kind === 'review' && state.task.project === reviewProject) {
        reviewActionPending = true; renderReviewState();
        try { await api('stop', {}); notice('正在停止重译，已完成章节会保留'); await poll(); }
        catch (error) { notice(error.message); }
        finally { reviewActionPending = false; renderReviewState(); }
        return;
    }
    if (!reviewData || !reviewSelected.size || reviewLoading) return;
    reviewActionPending = true; renderReviewState();
    try {
        const items = reviewData.items.filter(item => reviewSelected.has(item.id));
        await api('retranslate', {project: reviewProject, model: $('model').value,
            term_revision: reviewData.term_revision, items});
        reviewRunning = true; await poll();
    } catch (error) { notice(error.message); }
    finally { reviewActionPending = false; renderReviewState(); }
};
