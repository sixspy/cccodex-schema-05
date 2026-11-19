const msg = document.getElementById('msg');
const view = document.getElementById('view');
let contracts = [];
let residuals = [];
let lastCheck = null;

async function api(path, opts={}) {
  const r = await fetch(path, {
    headers: {'Content-Type': 'application/json', ...(opts.headers||{})},
    ...opts
  });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) {
    const err = new Error(j.error || ('HTTP '+r.status));
    err.status = r.status;
    err.data = j;
    throw err;
  }
  return j;
}

function setMsg(t, isErr=false) {
  msg.textContent = t || '';
  msg.className = isErr ? 'err' : 'muted';
}

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, ch => ({
    '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'
  }[ch]));
}

function markerChip(marker, extra='') {
  if (!marker) return '';
  return `<span class="chip chip-residual" title="半截换版残段">${esc(marker)}</span>`;
}

function occupyChip(r) {
  if (r.status === '已收口') return '<span class="chip chip-done">已收口</span>';
  if (r.occupies) return '<span class="chip chip-occupies">占库存</span>';
  return '<span class="chip">不占库存</span>';
}

async function loadContracts() {
  const data = await api('/api/contracts');
  contracts = data.items || [];
}

async function loadResiduals() {
  const data = await api('/api/residuals');
  residuals = data.items || [];
  return data;
}

function renderContracts() {
  view.innerHTML = `<div class="grid">${contracts.map(c => `
    <article class="card" data-cid="${c.id}">
      <div class="row">
        <h3>${esc(c.table_name)} · v${c.version_no}</h3>
        <span class="chip">${esc(c.status)}</span>
        ${markerChip(c.residual_marker)}
      </div>
      <p class="muted">${esc(c.note)} · 字段 ${c.field_count}</p>
      <button type="button" class="ghost btn-detail">详情</button>
      <div class="detail muted"></div>
    </article>`).join('')}</div>`;
  view.querySelectorAll('.btn-detail').forEach(btn => {
    btn.onclick = async () => {
      const card = btn.closest('.card');
      try {
        const j = await api(`/api/contracts/${card.dataset.cid}`);
        const c = j.contract;
        card.querySelector('.detail').innerHTML = `
          <p>${markerChip(c.residual_marker) || '无残段标记'}</p>
          <ul>${c.fields.map(f => `<li>${esc(f.field_name)} <span class="muted">${esc(f.field_type)}</span></li>`).join('')}</ul>`;
      } catch (e) { setMsg(e.message, true); }
    };
  });
}

function draftOptions() {
  return contracts.filter(c => c.status === '草稿')
    .map(c => `<option value="${c.id}">${esc(c.table_name)} v${c.version_no}</option>`).join('');
}

function applyPageItems() {
  // 换版附页：草稿 + 该表未收口残段一并列出，标记同源
  return residuals.filter(r => r.status !== '已收口').map(r => `
    <article class="card">
      <div class="row">
        <strong>${esc(r.table_name)}</strong>
        ${markerChip(r.marker)}
        ${occupyChip(r)}
      </div>
      <p class="muted">契约 v${r.half_version} 已升现行 / 目录 v${r.catalog_version ?? '—'}；取数：${esc(r.recovery_mode || '未择')}</p>
    </article>`).join('');
}

function renderCheck() {
  view.innerHTML = `
    <div class="card grid">
      <label>草稿契约
        <select id="cid">${draftOptions()}</select>
      </label>
      <button class="primary" id="btn-check" type="button">运行破坏性检查</button>
      <pre id="out" class="muted"></pre>
    </div>`;
  document.getElementById('btn-check').onclick = async () => {
    try {
      const cid = document.getElementById('cid').value;
      const j = await api(`/api/contracts/${cid}/breaking-check`, {method:'POST', body:'{}'});
      lastCheck = j.check;
      document.getElementById('out').textContent = JSON.stringify(j.check, null, 2);
      setMsg(`检查结论：${j.check.verdict}`);
    } catch (e) { setMsg(e.message, true); }
  };
}

function renderApply() {
  view.innerHTML = `
    <div class="card grid">
      <label>草稿契约
        <select id="cid">${draftOptions()}</select>
      </label>
      <div class="row">
        <button class="primary" id="btn-apply" type="button">换版应用</button>
        <button type="button" class="ghost" id="btn-apply-fault" title="演练换版中途抛错">演练中途抛错</button>
      </div>
    </div>
    <h3 class="section">换版附页 · 未收口残段</h3>
    <div class="grid" id="apply-residuals">${applyPageItems() || '<p class="muted">无未收口残段</p>'}</div>`;
  const doApply = async (fault) => {
    try {
      const cid = document.getElementById('cid').value;
      const payload = fault ? {fault} : {};
      const j = await api(`/api/contracts/${cid}/apply`, {
        method:'POST', body: JSON.stringify(payload)
      });
      if (j.half_applied) {
        setMsg(j.message, true);
      } else {
        setMsg(`已应用 ${j.contract.table_name} v${j.contract.version_no}`);
      }
      await loadContracts();
      await loadResiduals();
      renderApply();
    } catch (e) { setMsg(e.message, true); }
  };
  document.getElementById('btn-apply').onclick = () => doApply(false);
  document.getElementById('btn-apply-fault').onclick = () => doApply('after_contract');
}

function modeChip(r) {
  if (r.recovery_mode === '清掉') return '<span class="chip chip-clean">清掉</span>';
  if (r.recovery_mode === '快照') return '<span class="chip chip-snapshot">快照</span>';
  return '<span class="chip">未择取数</span>';
}

function renderResidualCard(r) {
  const settled = r.status === '已收口';
  const tasks = (r.tasks || []).map(t =>
    `<li>补目录 → v${t.target_version} · ${esc(t.status)}</li>`).join('');
  const log = (r.log || []).slice(-6).map(e =>
    `<li class="muted">${esc(e.step)}：${esc(e.detail)}</li>`).join('');
  return `
  <article class="card residual" data-rid="${r.id}">
    <div class="row">
      <strong>${esc(r.table_name)}</strong>
      ${markerChip(r.marker)}
      ${modeChip(r)}
      ${occupyChip(r)}
    </div>
    <p class="muted">契约 v${r.half_version}（${esc(r.half_status)}）/ 目录 v${r.catalog_version ?? '—'} · 取数：${esc(r.recovery_mode || '未择')}</p>
    ${r.blocked_reason ? `<p class="err">停机原因：${esc(r.blocked_reason)}</p>` : ''}
    <div class="row">
      <button type="button" class="btn-mode" data-mode="清掉" ${settled || r.recovery_mode ? 'disabled' : ''}>清掉（退回草稿）</button>
      <button type="button" class="btn-mode" data-mode="快照" ${settled || r.recovery_mode ? 'disabled' : ''}>快照（冻结补目录）</button>
      <button type="button" class="primary btn-resume" ${settled || !r.recovery_mode ? 'disabled' : ''}>续写落成</button>
      <button type="button" class="ghost btn-verify" ${!r.recovery_mode ? 'disabled' : ''}>复验一轮</button>
    </div>
    <ul>${tasks}</ul>
    <details><summary class="muted">编排记录</summary><ul>${log}</ul></details>
  </article>`;
}

async function renderResiduals() {
  const data = await loadResiduals();
  view.innerHTML = `
    <div class="card row">
      <span class="chip">残段 ${data.items.length}</span>
      <span class="chip ${data.pending_occupies ? 'chip-occupies' : ''}">占库存 ${data.occupies_total}</span>
      <span class="chip">未收口 ${data.open_total}</span>
      <span class="muted">半截行与契约态须同源，漂了就停</span>
    </div>
    <div class="grid">${data.items.map(renderResidualCard).join('') || '<p class="muted">暂无残段</p>'}</div>`;
  view.querySelectorAll('.residual').forEach(card => {
    const rid = card.dataset.rid;
    card.querySelectorAll('.btn-mode').forEach(btn => {
      btn.onclick = async () => {
        try {
          const j = await api(`/api/residuals/${rid}/mode`, {
            method:'POST', body: JSON.stringify({mode: btn.dataset.mode})
          });
          setMsg(`已取数：${j.residual.recovery_mode}（${j.residual.status}）`);
          await renderResiduals();
        } catch (e) { setMsg(e.message, true); }
      };
    });
    card.querySelector('.btn-resume').onclick = async () => {
      try {
        const j = await api(`/api/residuals/${rid}/resume`, {method:'POST', body:'{}'});
        const v = j.residual.verifications || {};
        setMsg(`续写落成：合同 ${j.residual.marker}；缺口复验${v.gap?.ok?'通过':'未过'}，补齐复验${v.patch?.ok?'通过':'未过'}`);
        await renderResiduals();
      } catch (e) { setMsg(e.message, true); await renderResiduals(); }
    };
    card.querySelector('.btn-verify').onclick = async () => {
      try {
        const j = await api(`/api/residuals/${rid}/verify`, {method:'POST', body:'{}'});
        setMsg(`${j.verification.marker}｜缺口：${j.verification.gap.detail}｜补齐：${j.verification.patch.detail}`);
      } catch (e) { setMsg(e.message, true); }
    };
  });
}

async function renderReconcile() {
  const reports = (await api('/api/reconcile-reports')).items || [];
  view.innerHTML = `
    <div class="card grid">
      <div class="row">
        <button class="primary" id="btn-rec" type="button">跑一轮对账</button>
        <span class="muted">对账卡片与残段列表同标记</span>
      </div>
      <div class="grid" id="reps">${reports.map(r => `
        <article class="card">
          <div class="row"><strong>${esc(r.as_of)}</strong>${markerChip(r.marker)}</div>
          <p>${esc(r.summary)}</p>
        </article>`).join('') || '<p class="muted">暂无对账</p>'}</div>
    </div>`;
  document.getElementById('btn-rec').onclick = async () => {
    try {
      const j = await api('/api/reconcile-reports', {
        method:'POST', body: JSON.stringify({as_of: '2026-11-19'})
      });
      setMsg(j.report.summary);
      await renderReconcile();
    } catch (e) { setMsg(e.message, true); }
  };
}

async function show(tab) {
  setMsg('');
  await loadContracts();
  await loadResiduals();
  if (tab === 'contracts') renderContracts();
  if (tab === 'check') renderCheck();
  if (tab === 'apply') renderApply();
  if (tab === 'residuals') await renderResiduals();
  if (tab === 'reconcile') await renderReconcile();
}

document.querySelectorAll('.pills button').forEach(btn => {
  btn.onclick = () => {
    document.querySelectorAll('.pills button').forEach(b => b.classList.remove('on'));
    btn.classList.add('on');
    show(btn.dataset.tab);
  };
});
document.getElementById('btn-logout').onclick = async () => {
  await api('/api/logout', {method:'POST', body:'{}'});
  location.href = '/login';
};
show('contracts');
