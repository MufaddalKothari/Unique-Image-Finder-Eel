'use strict';
/* Unique Image Finder - browser UI (talks to Python through Eel). No framework, no build step. */
(() => {
  const PAGE = 40;
  const THUMB = 192;
  const TABS = [
    { id: 'duplicates', label: 'Duplicates', which: 'work', selectLabel: 'Select all Working-folder copies',
      hint: 'Each row is a Reference ↔ Working match. “Select all” picks only the Working copy, never the Reference.' },
    { id: 'unique_ref', label: 'Uniques (Ref)', which: 'all', selectLabel: 'Select all', hint: 'Reference images with no match in the Working folder.' },
    { id: 'unique_work', label: 'Uniques (Work)', which: 'all', selectLabel: 'Select all', hint: 'Working images with no match in the Reference folder.' },
    { id: 'dups_ref', label: 'Duplicates in Ref', which: 'all_but_first', selectLabel: 'Select all except the first in each group',
      hint: 'Groups of look-alikes inside the Reference folder. “Select all” keeps the first of each group.' },
    { id: 'dups_work', label: 'Duplicates in Work', which: 'all_but_first', selectLabel: 'Select all except the first in each group',
      hint: 'Groups of look-alikes inside the Working folder. “Select all” keeps the first of each group.' },
  ];
  const FIELD_LABELS = { name: 'Name', size: 'Size', created: 'Created', mtime: 'Modified (mtime)', dimensions: 'Dimensions' };

  const S = {
    boot: null, jobId: null, tab: 'duplicates', counts: {}, selected: new Set(),
    loaded: 0, loadSeq: 0, running: false, modals: [],
  };

  // ---------------------------------------------------------------- tiny helpers
  const $ = (sel, root = document) => root.querySelector(sel);
  function h(tag, attrs, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === false || v == null) continue;
      if (k === 'class') el.className = v;
      else if (k === 'text') el.textContent = v;
      else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
      else if (k === 'checked' || k === 'disabled' || k === 'hidden') el[k] = !!v;
      else el.setAttribute(k, v === true ? '' : v);
    }
    for (const kid of kids.flat()) if (kid != null && kid !== false) el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
    return el;
  }
  const errText = (e) => (e && (e.errorText || e.message)) || String(e);
  const eelCall = (name, ...args) => window.eel[name](...args)();
  async function call(name, ...args) {
    try { return await eelCall(name, ...args); }
    catch (e) { toast(`${name}: ${errText(e)}`, 'error'); throw e; }
  }
  function toast(msg, kind = '') {
    const t = h('div', { class: 'toast ' + kind, text: msg });
    $('#toasts').append(t);
    setTimeout(() => t.remove(), kind === 'error' ? 9000 : 4500);
  }
  const fmtSize = (n) => {
    if (n == null) return '';
    const u = ['B', 'KB', 'MB', 'GB']; let i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return (i ? n.toFixed(1) : n) + ' ' + u[i];
  };
  const fmtDate = (sec) => (sec ? new Date(sec * 1000).toLocaleString() : '');
  const imgUrl = (path, px) => `/img?p=${encodeURIComponent(path)}&s=${px}`;
  const plural = (n, word) => `${n.toLocaleString()} ${word}${n === 1 ? '' : 's'}`;

  // ---------------------------------------------------------------- modals
  function modal({ title, body, footer, size = '', onClose }) {
    const root = $('#modal-root');
    const bodyEl = h('div', { class: 'modal-body' }, body);
    const footEl = h('div', { class: 'modal-foot' }, footer || []);
    const closeBtn = h('button', { class: 'btn small', text: '✕', 'aria-label': 'Close' });
    const box = h('div', { class: 'modal ' + size, role: 'dialog', 'aria-modal': 'true' },
      h('div', { class: 'modal-head' }, h('span', { text: title }), h('span', { class: 'spacer' }), closeBtn),
      bodyEl, footer ? footEl : null);
    const backdrop = h('div', { class: 'backdrop' }, box);
    const api = {
      el: box, body: bodyEl, foot: footEl,
      close(result) {
        const i = S.modals.indexOf(api);
        if (i < 0) return;
        S.modals.splice(i, 1);
        backdrop.remove();
        if (onClose) onClose(result);
      },
    };
    closeBtn.addEventListener('click', () => api.close(null));
    backdrop.addEventListener('mousedown', (e) => { if (e.target === backdrop) api.close(null); });
    S.modals.push(api);
    root.append(backdrop);
    return api;
  }
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && S.modals.length) S.modals[S.modals.length - 1].close(null);
  });

  function confirmDialog(message, okLabel = 'OK', danger = false) {
    return new Promise((resolve) => {
      const m = modal({
        title: 'Please confirm', size: 'narrow', onClose: (r) => resolve(r === true),
        body: h('p', { text: message }),
        footer: [h('button', { class: 'btn', text: 'Cancel', onclick: () => m.close(false) }),
                 h('button', { class: 'btn ' + (danger ? 'danger' : 'primary'), text: okLabel, onclick: () => m.close(true) })],
      });
    });
  }

  // Server-side folder picker: a browser cannot reveal absolute paths, so we browse on the Python side.
  function pickFolder(title, startPath) {
    return new Promise((resolve) => {
      const pathInput = h('input', { type: 'text', spellcheck: 'false', placeholder: 'Absolute folder path' });
      const list = h('div', { class: 'dir-list' });
      const note = h('div', { class: 'hint' });
      const selectBtn = h('button', { class: 'btn primary', text: 'Select this folder', disabled: true });
      let current = '';
      const m = modal({
        title, size: 'narrow', onClose: (r) => resolve(r || null),
        body: [h('div', { class: 'picker-bar' }, pathInput, h('button', { class: 'btn', text: 'Go', onclick: () => go(pathInput.value.trim()) })), list, note],
        footer: [h('button', { class: 'btn', text: 'Cancel', onclick: () => m.close(null) }), selectBtn],
      });
      async function go(path) {
        let data;
        try { data = await eelCall('list_dirs', path, false); } catch (e) { note.textContent = errText(e); return; }
        if (data.error) { note.textContent = data.error; return; }
        current = data.path;
        pathInput.value = current;
        selectBtn.disabled = !current;
        note.textContent = current ? `${plural(data.dirs.length, 'sub-folder')}` : 'Choose a starting point';
        list.replaceChildren();
        if (data.parent !== null) list.append(dirRow('⬑  .. (up)', data.parent, null));
        for (const d of data.dirs) list.append(dirRow('📁 ' + d.name, d.path, d.images));
        if (!data.dirs.length && data.parent === null) list.append(h('div', { class: 'empty', text: 'No folders' }));
      }
      const dirRow = (label, path, n) => h('div', { class: 'dir', onclick: () => go(path) },
        h('span', { text: label }), h('span', { class: 'n', text: n ? plural(n, 'image') : '' }));
      pathInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') go(pathInput.value.trim()); });
      selectBtn.addEventListener('click', () => current && m.close(current));
      go(startPath || '');
    });
  }

  // ---------------------------------------------------------------- jobs
  function pollJob(id, onUpdate) {
    return new Promise((resolve) => {
      const tick = async () => {
        let snap;
        try { snap = await eelCall('get_job', id); }
        catch (e) { return resolve({ state: 'error', error: errText(e), counts: {}, warnings: [] }); }
        if (onUpdate) onUpdate(snap);
        if (snap.state === 'running') setTimeout(tick, 250); else resolve(snap);
      };
      tick();
    });
  }

  // ---------------------------------------------------------------- settings / form
  function criteria() {
    const fields = [...document.querySelectorAll('#fields-list input:checked')].map((i) => i.value);
    return { hash: $('#use-hash').checked, fields, similarity: Number($('#similarity').value) };
  }
  function persist() {
    const c = criteria();
    call('save_settings', { last_ref: $('#ref-dir').value.trim(), last_work: $('#work-dir').value.trim(),
      similarity: c.similarity, use_hash: c.hash, fields: c.fields, theme: document.documentElement.dataset.theme }).catch(() => {});
  }
  function syncOptionState() {
    const useHash = $('#use-hash').checked;
    $('#similarity').disabled = !useHash;
    const bits = (S.boot ? S.boot.hash_size : 16) ** 2;
    const v = Number($('#similarity').value);
    const k = v >= 100 ? 0 : Math.round((1 - v / 100) * bits);
    const anyField = !!document.querySelector('#fields-list input:checked');
    $('#sim-label').textContent = useHash ? `${v}% (≤ ${k} of ${bits} bits differ)` : anyField ? 'matching by selected fields' : 'matching by file name';
    $('#fields-dd summary').textContent = (anyField ? `Compare fields (${document.querySelectorAll('#fields-list input:checked').length}) ▾` : 'Compare fields ▾');
  }
  function setTheme(theme) {
    document.documentElement.dataset.theme = theme;
    $('#theme-btn').textContent = theme === 'dark' ? '🌙' : '☀️';
  }

  // ---------------------------------------------------------------- progress / notices
  function notice(msg, kind = '') { $('#notices').append(h('div', { class: 'notice ' + kind, text: msg })); }
  function showProgress(snap) {
    $('#progress').value = snap.progress;
    $('#progress-msg').textContent = snap.message || '';
  }
  function setRunning(on, cancellable = true) {
    S.running = on;
    $('#search-btn').disabled = on;
    $('#progress-wrap').hidden = !on;
    $('#cancel-btn').hidden = !(on && cancellable);
    if (on) { $('#progress').value = 0; $('#progress-msg').textContent = 'Starting…'; }
  }

  async function runSearch() {
    if (S.running) return;
    $('#notices').replaceChildren();
    const ref = $('#ref-dir').value.trim(), work = $('#work-dir').value.trim();
    if (!ref || !work) { notice('Please choose both a Reference and a Working folder.', 'error'); return; }
    const c = criteria();
    const r = await call('start_search', ref, work, c);
    if (!r.ok) { notice(r.error, 'error'); return; }
    $('#ref-dir').value = r.ref; $('#work-dir').value = r.work;   // always show the absolute paths
    S.selected.clear(); updateSelected();
    S.jobId = r.job_id; S.counts = {};
    persist();
    setRunning(true);
    const snap = await pollJob(r.job_id, showProgress);
    setRunning(false);
    for (const w of snap.warnings || []) notice(w);
    if (snap.state === 'error') notice('Search failed: ' + snap.error, 'error');
    else if (snap.state === 'cancelled') notice('Search cancelled.');
    else if (snap.summary) {
      const s = snap.summary;
      if (c.hash) notice(`Indexed ${plural(s.ref_files + s.work_files, 'image')}: ${s.cached.toLocaleString()} from cache, ${s.computed.toLocaleString()} newly hashed${s.failed ? `, ${s.failed} unreadable` : ''}.`);
    }
    S.counts = snap.counts || {};
    updateTabs();
    await loadTab(true);
  }

  // ---------------------------------------------------------------- tabs / list
  function buildTabs() {
    const nav = $('#tabs');
    nav.replaceChildren(...TABS.map((t) => h('button', { class: 'tab', role: 'tab', 'data-tab': t.id, onclick: () => selectTab(t.id) })));
    updateTabs();
  }
  function updateTabs() {
    for (const t of TABS) {
      const b = $(`.tab[data-tab="${t.id}"]`);
      b.textContent = `${t.label} (${(S.counts[t.id] || 0).toLocaleString()})`;
      b.classList.toggle('active', t.id === S.tab);
    }
    const def = TABS.find((t) => t.id === S.tab);
    $('#select-all-label').textContent = def.selectLabel;
    $('#tab-hint').textContent = def.hint;
    $('#select-all').checked = false;
  }
  function selectTab(id) { S.tab = id; updateTabs(); loadTab(true); }

  function updateSelected() { $('#selected-count').textContent = `Selected: ${S.selected.size.toLocaleString()}`; }
  function syncChecks() {
    document.querySelectorAll('#list input[data-path]').forEach((cb) => { cb.checked = S.selected.has(cb.dataset.path); });
  }
  function pathCheck(path) {
    return h('input', { type: 'checkbox', 'data-path': path, checked: S.selected.has(path), onchange: (e) => {
      if (e.target.checked) S.selected.add(path); else S.selected.delete(path);
      updateSelected();
    } });
  }
  function thumb(f, onclick) {
    const img = h('img', { class: 'thumb', loading: 'lazy', alt: '', src: imgUrl(f.path, THUMB), onclick });
    img.addEventListener('error', () => { img.style.visibility = 'hidden'; });
    return img;
  }
  function fileInfo(f, extra) {
    const dims = f.width ? `${f.width} × ${f.height}` : '';
    return h('div', { class: 'info' },
      h('div', { class: 'name', text: f.name }),
      h('div', { class: 'muted', text: [fmtSize(f.size), dims, fmtDate(f.mtime)].filter(Boolean).join('  ·  ') }),
      h('div', { text: f.path }), extra);
  }
  function openButtons(f) {
    return h('div', { class: 'side' },
      h('button', { class: 'btn small', text: 'Open', onclick: async () => { if (!(await eelCall('open_file', f.path, false))) toast('Could not open file', 'error'); } }),
      h('button', { class: 'btn small', text: 'Show in folder', onclick: async () => { if (!(await eelCall('open_file', f.path, true))) toast('Could not open folder', 'error'); } }));
  }

  function duplicateCard(it) {
    const open = () => openCompare(it);
    return h('div', { class: 'card' },
      h('div', { class: 'col' }, h('span', { class: 'tag', text: 'Ref' }), pathCheck(it.ref.path)), thumb(it.ref, open),
      h('div', { class: 'col' }, h('span', { class: 'tag', text: 'Work' }), pathCheck(it.work.path)), thumb(it.work, open),
      h('div', { class: 'info' },
        h('div', { text: 'Ref:  ' + it.ref.path }), h('div', { text: 'Work: ' + it.work.path }),
        h('div', { class: 'muted', text: 'Match: ' + it.reason })),
      h('div', { class: 'side' }, h('button', { class: 'btn small', text: 'Compare', onclick: open })));
  }
  function uniqueCard(f) {
    return h('div', { class: 'card' }, pathCheck(f.path), thumb(f, () => openPreview(f)), fileInfo(f), openButtons(f));
  }
  function groupCard(g, n) {
    return h('div', { class: 'group' },
      h('div', { class: 'group-head', text: `Group ${n} — ${g.files.length} files${g.max_dist ? ` (up to ${g.max_dist} bits apart)` : ' (identical)'}` }),
      g.files.map((f, i) => h('div', { class: 'card' }, pathCheck(f.path), thumb(f, () => openPreview(f)),
        fileInfo(f, i === 0 ? h('div', { class: 'muted', text: 'first in group (kept by “Select all”)' }) : null), openButtons(f))));
  }

  async function loadTab(reset) {
    const seq = ++S.loadSeq;
    const list = $('#list');
    if (reset) { list.replaceChildren(); S.loaded = 0; $('#more-btn').hidden = true; list.scrollTop = 0; }
    if (!S.jobId) { list.append(h('div', { class: 'empty', text: 'Choose two folders and press Search.' })); return; }
    const res = await call('get_page', S.jobId, S.tab, S.loaded, PAGE);
    if (seq !== S.loadSeq) return;                       // user switched tab meanwhile
    S.counts = res.counts; updateTabs();
    const total = S.counts[S.tab] || 0;
    if (!res.items.length && S.loaded === 0) {
      list.append(h('div', { class: 'empty', text: S.running ? 'Searching…' : 'Nothing here.' }));
      return;
    }
    res.items.forEach((it, i) => {
      list.append(S.tab === 'duplicates' ? duplicateCard(it)
        : S.tab.startsWith('dups_') ? groupCard(it, S.loaded + i + 1) : uniqueCard(it));
    });
    S.loaded += res.items.length;
    $('#more-btn').hidden = S.loaded >= total;
  }

  // ---------------------------------------------------------------- previews / compare
  function openPreview(f) {
    modal({ title: f.name, size: 'wide', body: [h('div', { class: 'cmp-images' }, h('figure', null, h('img', { src: imgUrl(f.path, 1400), alt: '' }))),
      h('p', { class: 'hint', text: f.path })] });
  }

  async function openCompare(it) {
    const rows = [['Name', 'name'], ['Size (bytes)', 'size'], ['Path', 'path'], ['Dimensions (W×H)', 'dimensions'], ['Mode', 'mode'],
      ['Created (fs)', 'created', true], ['EXIF DateTimeOriginal', 'datetime_original'], ['Artist / Author', 'artist'], ['Copyright', 'copyright'],
      ['Camera make', 'make'], ['Camera model', 'model'], ['Image description', 'image_description'], ['Origin', 'origin'], ['Modified (mtime)', 'mtime', true]];
    const table = h('table', { class: 'meta' }, h('tbody', null, h('tr', null, h('th'), h('th', { text: 'Reference' }), h('th', { text: 'Working' }))));
    const act = async (paths) => {
      if (paths.length) {
        const label = paths.length === 2 ? 'both files' : paths[0] === it.ref.path ? 'the Reference file' : 'the Working file';
        if (!(await confirmDialog(`Move ${label} to the Trash?`, 'Delete', true))) return;
        const res = await call('delete_paths', S.jobId, paths);
        afterFileOp(res, 'moved to Trash');
      }
      m.close(null);
    };
    const m = modal({
      title: 'Image comparison', size: 'wide',
      body: [h('div', { class: 'cmp-images' },
        h('figure', null, h('figcaption', { text: 'Reference' }), h('img', { src: imgUrl(it.ref.path, 1100), alt: '' })),
        h('figure', null, h('figcaption', { text: 'Working' }), h('img', { src: imgUrl(it.work.path, 1100), alt: '' }))),
        table, h('p', { class: 'reason', text: 'Match: ' + it.reason })],
      footer: [h('button', { class: 'btn danger', text: 'Delete both', onclick: () => act([it.ref.path, it.work.path]) }),
               h('button', { class: 'btn danger', text: 'Delete Reference', onclick: () => act([it.ref.path]) }),
               h('button', { class: 'btn danger', text: 'Delete Working', onclick: () => act([it.work.path]) }),
               h('button', { class: 'btn success', text: 'Keep both', onclick: () => m.close(null) })],
    });
    try {
      const meta = await eelCall('get_meta', it.ref.path, it.work.path);
      const body = table.tBodies[0];
      for (const [label, key, isDate] of rows) {
        const fmt = (v) => (v == null || v === '' ? '' : isDate ? fmtDate(v) : String(v));
        const a = fmt(meta.a[key]), b = fmt(meta.b[key]);
        const same = a !== '' && a === b;
        body.append(h('tr', null, h('th', { text: label }), h('td', { class: same ? 'same' : '', text: a }), h('td', { class: same ? 'same' : '', text: b })));
      }
    } catch (e) { table.after(h('p', { class: 'hint', text: 'Metadata unavailable: ' + errText(e) })); }
  }

  // ---------------------------------------------------------------- file actions
  function afterFileOp(res, verb) {
    for (const p of res.done) S.selected.delete(p);
    updateSelected();
    if (res.counts && Object.keys(res.counts).length) { S.counts = res.counts; updateTabs(); }
    if (res.done.length) toast(`${plural(res.done.length, 'file')} ${verb}.`);
    if (res.errors.length) toast(`${plural(res.errors.length, 'file')} failed: ${res.errors[0].error}`, 'error');
    loadTab(true);
  }
  const needJob = () => { if (!S.jobId) { toast('Run a search first.'); return false; } return true; };

  async function deleteDuplicatesFromWorking() {
    if (!needJob()) return;
    const paths = await call('get_tab_paths', S.jobId, 'duplicates', 'work');
    if (!paths.length) { toast('No duplicates to delete.'); return; }
    if (!(await confirmDialog(`Move ${plural(paths.length, 'Working-folder file')} that duplicate a Reference image to the Trash?`, 'Move to Trash', true))) return;
    afterFileOp(await call('delete_paths', S.jobId, paths), 'moved to Trash');
  }
  async function keepDuplicates() {
    if (!needJob()) return;
    if (!(S.counts.duplicates > 0)) { toast('No duplicates to hide.'); return; }
    S.counts = await call('dismiss_tab', S.jobId, 'duplicates');
    updateTabs(); loadTab(true);
    toast('Duplicate list cleared; no files were touched.');
  }
  async function saveUniques() {
    if (!needJob()) return;
    if (!((S.counts.unique_ref || 0) + (S.counts.unique_work || 0))) { toast('No uniques to save.'); return; }
    const dest = await pickFolder('Copy uniques into…', (S.boot.settings || {}).last_dest);
    if (!dest) return;
    const res = await call('copy_uniques', S.jobId, dest);
    toast(`Copied ${plural(res.done.length, 'file')} to ${dest}`);
    if (res.errors.length) toast(`${res.errors.length} failed: ${res.errors[0].error}`, 'error');
  }
  async function moveSelected() {
    if (!needJob()) return;
    if (!S.selected.size) { toast('Nothing selected.'); return; }
    const dest = await pickFolder('Move selected files to…', (S.boot.settings || {}).last_dest);
    if (!dest) return;
    afterFileOp(await call('move_paths', S.jobId, [...S.selected], dest), 'moved');
  }
  async function deleteSelected() {
    if (!needJob()) return;
    if (!S.selected.size) { toast('Nothing selected.'); return; }
    if (!(await confirmDialog(`Move ${plural(S.selected.size, 'selected file')} to the Trash?`, 'Move to Trash', true))) return;
    afterFileOp(await call('delete_paths', S.jobId, [...S.selected]), 'moved to Trash');
  }
  async function onSelectAll(e) {
    if (!S.jobId) { e.target.checked = false; return; }
    const def = TABS.find((t) => t.id === S.tab);
    const paths = await call('get_tab_paths', S.jobId, S.tab, def.which);
    for (const p of paths) { if (e.target.checked) S.selected.add(p); else S.selected.delete(p); }
    updateSelected(); syncChecks();
  }

  // ---------------------------------------------------------------- info + cache manager
  function openHashInfo() {
    modal({ title: 'About hashing and the cache', size: 'narrow', footer: null, body: [
      h('h3', { text: 'Difference hash (dHash)' }),
      h('p', { text: 'Each image is reduced to a small grayscale grid and encoded by whether each pixel is brighter than its neighbour. Resized, re-compressed or lightly edited copies end up with almost the same bits.' }),
      h('h3', { text: 'Similarity' }),
      h('p', { text: 'The slider is the share of hash bits that must agree. 100% means identical hashes; lower values allow that many bits to differ (shown next to the slider). Start around 90%; use 97–99% for near-exact copies.' }),
      h('h3', { text: 'The cache' }),
      h('p', { text: 'Every hash is stored in a local SQLite database (B-tree indexed) under the file’s absolute path, with its size and modified time. Unchanged files are never re-opened on later searches; changed files are re-hashed automatically. Use the Cache button to see, refresh or forget what is stored.' }),
    ] });
  }

  async function openCacheManager() {
    const statsEl = h('div', { class: 'stats' });
    const tbody = h('tbody');
    const status = h('div', { class: 'hint' });
    const prog = h('progress', { max: 100, value: 0, hidden: true });
    const buttons = [];
    const lock = (on) => buttons.forEach((b) => { b.disabled = on; });
    const btn = (text, cls, fn) => { const b = h('button', { class: 'btn ' + cls, text, onclick: fn }); buttons.push(b); return b; };

    async function refresh() {
      const info = await call('cache_info');
      const s = info.stats;
      statsEl.replaceChildren(
        h('span', null, 'Images cached: ', h('b', { text: s.images.toLocaleString() })),
        h('span', null, 'Hashed: ', h('b', { text: s.hashed.toLocaleString() })),
        h('span', null, 'Unreadable: ', h('b', { text: s.failed.toLocaleString() })),
        h('span', null, 'Database: ', h('b', { text: fmtSize(s.db_bytes) })),
        h('span', { text: s.db_path }));
      tbody.replaceChildren(...(info.roots.length ? info.roots.map((r) => h('tr', null,
        h('td', { text: r.path + (r.exists ? '' : '  (missing)') }),
        h('td', { text: `${r.cached.toLocaleString()} / ${r.file_count.toLocaleString()}` }),
        h('td', { text: fmtDate(r.last_scan) }),
        h('td', null, h('span', { class: 'side', style: 'flex-direction:row' },
          btn('Refresh', 'small', () => reindex(r.path, false)), btn('Rehash', 'small', () => reindex(r.path, true)),
          btn('Forget', 'small', async () => {
            if (await confirmDialog(`Remove everything cached under ${r.path}? Files on disk are not touched.`, 'Forget', true)) { await call('cache_forget', r.path); refresh(); }
          }))))) : [h('tr', null, h('td', { colspan: 4, class: 'hint', text: 'No folders cached yet. Run a hash search or add a folder.' }))]));
    }
    async function reindex(path, force) {
      const r = await call('cache_index', path, force);
      if (!r.ok) { toast(r.error, 'error'); return; }
      lock(true); prog.hidden = false;
      const snap = await pollJob(r.job_id, (s) => { prog.value = s.progress; status.textContent = s.message; });
      prog.hidden = true; lock(false);
      status.textContent = snap.state === 'error' ? 'Failed: ' + snap.error : snap.message;
      refresh();
    }
    const m = modal({
      title: 'Hash cache', size: 'wide',
      body: [statsEl, h('div', { class: 'row' }, prog, status),
        h('table', { class: 'cache' }, h('thead', null, h('tr', null, ['Folder', 'Cached / found', 'Last scan', ''].map((t) => h('th', { text: t })))), tbody)],
      footer: [
        btn('Add folder…', 'success', async () => { const p = await pickFolder('Index a folder', (S.boot.settings || {}).picker_start); if (p) reindex(p, false); }),
        btn('Purge missing files', '', async () => { const n = await call('cache_purge_missing'); toast(`Removed ${plural(n, 'stale entry')}.`); refresh(); }),
        btn('Clear all', 'danger', async () => { if (await confirmDialog('Delete the entire hash cache? Files on disk are not touched; the next search will re-hash.', 'Clear', true)) { await call('cache_clear'); refresh(); } }),
        h('span', { class: 'spacer' }),
        h('button', { class: 'btn', text: 'Close', onclick: () => m.close(null) })],
    });
    refresh();
  }

  // ---------------------------------------------------------------- init
  async function browseInto(inputId) {
    const input = $(inputId);
    const dir = await pickFolder('Choose a folder', input.value.trim() || (S.boot.settings || {}).picker_start || '');
    if (dir) { input.value = dir; persist(); }
  }

  async function init() {
    S.boot = await call('get_bootstrap');
    const s = S.boot.settings || {};
    setTheme(s.theme || (window.matchMedia && matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'));
    $('#hash-size').textContent = $('#hash-size2').textContent = S.boot.hash_size;
    $('#fields-list').append(...S.boot.fields.map((k) => h('label', { class: 'check' },
      h('input', { type: 'checkbox', value: k, checked: (s.fields || []).includes(k) }), FIELD_LABELS[k] || k)));
    $('#ref-dir').value = s.last_ref || '';
    $('#work-dir').value = s.last_work || '';
    $('#use-hash').checked = s.use_hash !== false && !(s.fields || []).length;
    $('#similarity').value = s.similarity || 90;
    buildTabs();
    syncOptionState();
    loadTab(true);

    $('#fields-list').addEventListener('change', () => {
      if (document.querySelector('#fields-list input:checked')) $('#use-hash').checked = false;
      syncOptionState();
    });
    $('#use-hash').addEventListener('change', (e) => {
      if (e.target.checked) document.querySelectorAll('#fields-list input').forEach((i) => { i.checked = false; });
      syncOptionState();
    });
    document.addEventListener('click', (e) => { const dd = $('#fields-dd'); if (dd.open && !dd.contains(e.target)) dd.open = false; });
    $('#similarity').addEventListener('input', syncOptionState);
    $('#similarity').addEventListener('change', persist);
    $('#search-btn').addEventListener('click', runSearch);
    $('#cancel-btn').addEventListener('click', () => { if (S.jobId) call('cancel_job', S.jobId); });
    $('#ref-browse').addEventListener('click', () => browseInto('#ref-dir'));
    $('#work-browse').addEventListener('click', () => browseInto('#work-dir'));
    for (const id of ['#ref-dir', '#work-dir']) $(id).addEventListener('keydown', (e) => { if (e.key === 'Enter') runSearch(); });
    $('#theme-btn').addEventListener('click', () => { setTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark'); persist(); });
    $('#hash-info-btn').addEventListener('click', openHashInfo);
    $('#cache-btn').addEventListener('click', openCacheManager);
    $('#select-all').addEventListener('change', onSelectAll);
    $('#more-btn').addEventListener('click', () => loadTab(false));
    $('#list').addEventListener('scroll', (e) => {
      const l = e.target;
      if (!$('#more-btn').hidden && l.scrollTop + l.clientHeight >= l.scrollHeight - 300) loadTab(false);
    });
    $('#delete-dups-btn').addEventListener('click', deleteDuplicatesFromWorking);
    $('#keep-btn').addEventListener('click', keepDuplicates);
    $('#save-uniques-btn').addEventListener('click', saveUniques);
    $('#move-btn').addEventListener('click', moveSelected);
    $('#delete-btn').addEventListener('click', deleteSelected);
  }

  window.__uif = { S, init };          // handy for debugging / tests
  init().catch((e) => { document.body.prepend(h('div', { class: 'notice error', text: 'Failed to start: ' + errText(e) })); });
})();
