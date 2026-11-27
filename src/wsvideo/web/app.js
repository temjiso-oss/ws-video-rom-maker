'use strict';
// WS Video ROM Maker の画面。Python 側 (webgui.py の Api) を window.pywebview.api で呼ぶ。
// pywebview がない (ブラウザーで開いた) ときは、見た目の確認用に見本のデータで動く。
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const icon = (name, cls = '') => `<svg class="ic ${cls}"><use href="#i-${name}"/></svg>`;

const STATUS = {
  wait: ['wait', '待ち', 'circle'],
  run: ['run', '変換中', 'clock'],
  done: ['done', '完了', 'check'],
  error: ['error', 'エラー', 'alert'],
  cancel: ['cancel', '中止', 'stop'],
};

const app = {
  api: null,
  info: null,
  view: null,
  editing: null,
  trimError: '',
  logFrom: 0,
  timer: null,
  showLog: false,
  finishedShown: false,

  async boot() {
    this.api = await waitApi();
    this.info = await this.api.init();
    $('ver').textContent = this.info.version;
    const dev = $('device');
    $('device-name').textContent = this.info.gpu ? `GPU：${this.info.gpu}` : 'CPU で計算';
    dev.classList.toggle('gpu', !!this.info.gpu);
    this.fillSelects();
    this.bind();
    this.render(this.info.view);
    if (this.info.view.running) this.startPolling();
  },

  // ---- 表示 ---------------------------------------------------------------
  render(v) {
    this.view = v;
    const empty = v.items.length === 0;
    $('videos').classList.toggle('empty', empty);
    $('count').textContent = empty ? '' : `${v.items.length} 本`;
    $('btn-clear').hidden = empty || v.running;
    $('btn-add').hidden = v.running;
    this.renderList(v);
    this.renderPresets(v);
    $('outdir').textContent = v.outdir || '（動画を入れると、動画と同じフォルダーになります）';
    $('outdir').title = v.outdir || '';
    for (const b of $('seg-combine').children) b.classList.toggle('on', (b.dataset.v === '1') === v.combine);
    $('chk-preview').checked = !!v.state.preview;
    $('chk-emu').checked = !!v.state.emu;
    $('lbl-emu').hidden = v.state.target !== 'nile';
    this.renderMetrics(v);
    this.renderDrawer(v.state);
    const busy = v.running;
    for (const id of ['btn-outdir', 'chk-preview', 'chk-emu', 'btn-settings']) $(id).disabled = busy;
    for (const b of $('seg-combine').children) b.disabled = busy;
  },

  renderList(v) {
    const busy = v.running;
    $('list').innerHTML = v.items.map((it, i) => {
      const [cls, label, ic] = STATUS[it.status] || STATUS.wait;
      const pct = it.status === 'run' ? ` ${Math.round(it.frac * 100)}%` : '';
      const editing = this.editing === it.id;
      const thumb = it.thumb ? `style="background-image:url('${it.thumb}')"` : '';
      return `<div class="row-item ${it.status}" data-id="${it.id}">
        <div class="thumb" ${thumb}>${it.thumb ? '' : icon('film')}</div>
        <div class="it-main">
          <div class="it-title" title="${esc(it.title)}">${esc(it.title)}</div>
          <div class="it-meta"><span>${esc(it.duration)}</span><span>${esc(it.desc)}</span>
            <button class="trim-btn" data-act="trim" ${busy ? 'disabled' : ''}>${icon('scissors')}切り出し：${esc(it.trim)}</button></div>
          ${editing ? `<div class="trim-edit">始まり <input id="t-start" placeholder="0:00" value="${esc(it.start)}"> 終わり <input id="t-end" placeholder="最後まで" value="${esc(it.length ? addTime(it.start, it.length) : '')}">
            <button class="btn small primary" data-act="trim-ok">反映</button><button class="btn small ghost" data-act="trim-cancel">やめる</button>
            <span class="muted">90 や 1:30 の形。終わりが空なら最後まで</span>${this.trimError ? `<span class="err">${esc(this.trimError)}</span>` : ''}</div>` : ''}
          ${it.status === 'error' && it.error ? `<div class="it-meta" style="color:var(--bad)">${esc(it.error)}</div>` : ''}
        </div>
        <div class="it-side">
          <span class="chip ${cls}" data-chip>${icon(ic)}<span data-chip-text>${label}${pct}</span></span>
          <div class="it-actions" ${busy ? 'hidden' : ''}>
            <button class="btn ghost" data-act="up" aria-label="上へ" ${i === 0 ? 'disabled' : ''}>${icon('up')}</button>
            <button class="btn ghost" data-act="down" aria-label="下へ" ${i === v.items.length - 1 ? 'disabled' : ''}>${icon('down')}</button>
            <button class="btn ghost" data-act="remove" aria-label="消す">${icon('x')}</button>
          </div>
        </div>
        <div class="row-bar" data-bar style="width:${it.status === 'run' ? it.frac * 100 : 0}%"></div>
      </div>`;
    }).join('');
  },

  renderPresets(v) {
    const pre = this.info.presets;
    $('presets').innerHTML = pre.map((p) => `<button class="preset ${v.preset === p.id ? 'on' : ''}" data-preset="${p.id}" ${v.running ? 'disabled' : ''}>
      ${icon(v.preset === p.id ? 'check-circle' : 'circle')}
      <div><div class="p-title">${esc(p.title)}${p.badge ? `<span class="pill">${esc(p.badge)}</span>` : ''}</div>
      <div class="p-sub">${esc(p.sub)}</div></div></button>`).join('');
    $('custom-pill').hidden = v.preset !== 'custom';
  },

  renderMetrics(v) {
    const e = v.estimate;
    const has = e.count > 0;
    $('m-time').textContent = has ? e.seconds : '—';
    $('m-time-note').textContent = has ? `${e.count} 本の動画` : '';
    $('m-fps').textContent = `${e.fps} fps`;
    $('m-fps-note').textContent = e.hz === '60Hz' ? '60Hz モード' : '75Hz（本来の速さ）';
    $('m-data-label').textContent = e.data.label;
    $('m-data').textContent = has ? e.data.value : '—';
    $('m-data-note').textContent = has ? e.data.note : '';
    $('m-data').closest('.metric').classList.toggle('warn', !!e.data.warn && has);
    $('m-eta').textContent = has ? `約 ${e.eta}` : '—';
    $('m-eta-note').textContent = has ? `${e.device} で計算` : '';
    const n = e.count;
    $('hint').textContent = !has ? '動画を入れると、ここに変換の見込みが出ます。'
      : (v.combine || n === 1) ? `${n === 1 ? '1 つの' : `${n} 本をまとめて 1 つの`} ROM を作ります。`
      : `${n} 本の動画から、動画ごとに別の ROM（${n} 個）を作ります。`;
  },

  fillSelects() {
    const c = this.info.choices;
    const fill = (id, list) => { $(id).innerHTML = list.map((o) => `<option value="${esc(String(o.value))}">${esc(o.label)}</option>`).join(''); };
    fill('s-sd', c.sd); fill('s-rom', c.rom); fill('s-quality', c.quality); fill('s-pal16', c.pal16);
    fill('s-dither', c.dither); fill('s-mode', c.mode); fill('s-audio', c.audio); fill('s-speaker', c.speaker);
    fill('s-vmode', c.vmode); fill('s-amode', c.amode);
  },

  renderDrawer(st) {
    const c = this.info.choices;
    $('s-k').innerHTML = c.fps.map((o) => `<option value="${o.value}">${st.hz60 ? o.label60 : o.label}</option>`).join('');
    const set = (id, val) => { const el = $(id); if (el.tagName === 'SELECT') el.value = String(val); else if (el.type === 'checkbox') el.checked = !!val; else el.value = val; };
    set('s-target', st.target); set('s-sd', st.sd); set('s-rom', st.rom); set('s-hz60', st.hz60); set('s-k', st.k);
    set('s-quality', st.quality); set('s-pal16', st.pal16 ? 'true' : 'false'); set('s-dither', st.dither); set('s-mode', st.mode);
    set('s-bright', st.bright); set('s-contrast', st.contrast); set('s-sat', st.sat); set('s-deint', st.deint);
    set('s-audio', st.audio); set('s-speaker', st.speaker); set('s-norm', st.norm);
    $('v-bright').textContent = Number(st.bright).toFixed(2);
    $('v-contrast').textContent = Number(st.contrast).toFixed(2);
    $('v-sat').textContent = Number(st.sat).toFixed(2);
    set('s-vmode', st.vmode); set('s-amode', st.amode);
    $('row-sd').hidden = st.target !== 'nile';
    $('row-rom').hidden = st.target !== 'rom';
    $('row-vmode').hidden = st.target !== 'nile';     // 描き方・音の形式は nileswan 版だけ
    $('row-amode').hidden = st.target !== 'nile' || !st.audio;
  },

  // ---- 操作 ---------------------------------------------------------------
  bind() {
    const add = async () => { const r = await this.api.pick_files(); this.render(r.view); if (r.bad && r.bad.length) this.toast('読み込めなかったファイル：' + r.bad.join(' / ')); };
    $('btn-add').onclick = add;
    $('btn-add2').onclick = add;
    $('btn-clear').onclick = async () => { if (await this.confirm('すべて消す', '一覧の動画をすべて消しますか？（動画のファイルは消えません）', 'すべて消す')) this.render(await this.api.clear()); };
    $('list').onclick = (e) => this.onListClick(e);
    $('list').onkeydown = (e) => { if (e.key === 'Enter' && (e.target.id === 't-start' || e.target.id === 't-end')) this.applyTrim(); };
    $('presets').onclick = async (e) => { const b = e.target.closest('[data-preset]'); if (b) this.render(await this.api.set_preset(b.dataset.preset)); };
    $('btn-outdir').onclick = async () => this.render(await this.api.choose_outdir());
    $('seg-combine').onclick = async (e) => { const b = e.target.closest('button'); if (b) this.render(await this.api.set_combine(b.dataset.v === '1')); };
    $('chk-preview').onchange = async (e) => this.render(await this.api.set_state({ preview: e.target.checked }));
    $('chk-emu').onchange = async (e) => this.render(await this.api.set_state({ emu: e.target.checked }));
    $('btn-start').onclick = () => this.start();
    $('btn-cancel').onclick = async () => { if (await this.confirm('中止', '変換を中止しますか？ ここまでの分は保存されません。', '中止する', true)) await this.api.cancel(); };
    $('btn-log').onclick = () => { this.showLog = !this.showLog; $('log').hidden = !this.showLog; };
    $('btn-log2').onclick = () => { $('log2').hidden = !$('log2').hidden; };
    $('btn-open').onclick = () => this.api.open_folder();
    $('btn-again').onclick = () => { $('done').hidden = true; $('metrics').hidden = false; $('actionbar').hidden = false; this.refresh(); };
    $('btn-settings').onclick = () => this.drawer(true);
    $('btn-close-drawer').onclick = () => this.drawer(false);
    $('drawer-back').onclick = () => this.drawer(false);
    $('btn-reset').onclick = async () => { if (await this.confirm('標準の設定に戻す', '詳細設定をすべて標準に戻しますか？', '戻す')) this.render(await this.api.reset_state()); };
    $('btn-about').onclick = async () => {
      const a = await this.api.about();
      this.message(`${a.app} ${a.version}`, `作者：BW　ライセンス：MIT\n変換の計算：${a.gpu ? 'GPU（' + a.gpu + '）' : 'CPU'}\n\n同梱の FFmpeg は GNU GPL v3 です。詳しくはアプリのフォルダーの THIRD_PARTY_NOTICES.md と licenses フォルダーを見てください。\n\n非公式のツールです。株式会社バンダイ、nileswan の開発者とは関係ありません。「ワンダースワン」「スワンクリスタル」は株式会社バンダイの商標です。\n\n本体の操作：START/A＝一時停止　Y2/Y4＝10 秒進む／戻る　Y1/Y3＝1 分進む／戻る　X2/X4＝次／前の動画　X1/X3＝スピーカーの音量`);
    };
    const ch = (id, key, conv) => { $(id).onchange = async (e) => this.render(await this.api.set_state({ [key]: conv(e.target) })); };
    ch('s-target', 'target', (t) => t.value); ch('s-sd', 'sd', (t) => +t.value); ch('s-rom', 'rom', (t) => +t.value);
    ch('s-hz60', 'hz60', (t) => t.checked); ch('s-k', 'k', (t) => +t.value); ch('s-quality', 'quality', (t) => t.value);
    ch('s-pal16', 'pal16', (t) => t.value === 'true'); ch('s-dither', 'dither', (t) => +t.value); ch('s-mode', 'mode', (t) => t.value);
    ch('s-deint', 'deint', (t) => t.checked); ch('s-audio', 'audio', (t) => +t.value); ch('s-speaker', 'speaker', (t) => +t.value);
    ch('s-norm', 'norm', (t) => t.checked);
    ch('s-vmode', 'vmode', (t) => t.value); ch('s-amode', 'amode', (t) => t.value);
    for (const k of ['bright', 'contrast', 'sat']) {
      $('s-' + k).oninput = (e) => { $('v-' + k).textContent = Number(e.target.value).toFixed(2); };
      ch('s-' + k, k, (t) => +t.value);
    }
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') { this.drawer(false); } });
    let depth = 0;
    document.addEventListener('dragenter', (e) => { e.preventDefault(); depth++; document.body.classList.add('dragging'); });
    document.addEventListener('dragleave', () => { depth = Math.max(0, depth - 1); if (!depth) document.body.classList.remove('dragging'); });
    document.addEventListener('dragover', (e) => e.preventDefault());
    document.addEventListener('drop', (e) => { e.preventDefault(); depth = 0; document.body.classList.remove('dragging'); });
  },

  async onListClick(e) {
    const b = e.target.closest('[data-act]');
    if (!b || b.disabled) return;
    const id = +b.closest('[data-id]').dataset.id;
    const act = b.dataset.act;
    if (act === 'up' || act === 'down') this.render(await this.api.move(id, act === 'up' ? -1 : 1));
    else if (act === 'remove') { if (this.editing === id) this.editing = null; this.render(await this.api.remove(id)); }
    else if (act === 'trim') { this.editing = this.editing === id ? null : id; this.trimError = ''; this.renderList(this.view); const s = $('t-start'); if (s) s.focus(); }
    else if (act === 'trim-cancel') { this.editing = null; this.trimError = ''; this.renderList(this.view); }
    else if (act === 'trim-ok') this.applyTrim();
  },

  async applyTrim() {
    const id = this.editing;
    if (id == null) return;
    const r = await this.api.set_trim(id, $('t-start').value, $('t-end').value);
    if (r.error) { this.trimError = r.error; this.renderList(this.view); return; }
    this.editing = null; this.trimError = '';
    this.render(r.view);
  },

  drawer(on) {
    if (on && this.view && this.view.running) return;
    $('drawer').hidden = !on;
    $('drawer-back').hidden = !on;
  },

  async refresh() { this.render(await this.api.view()); },

  // ---- 変換 ---------------------------------------------------------------
  async start() {
    const c = await this.api.check_start();
    if (!c.ok) { this.toast(c.error); return; }
    let toNile = false;
    if (c.rom_problem) {
      const r = await this.confirm('ROM に入りきりません', c.rom_problem + '\n\n「nileswan・60Hz の本体」（microSD から再生、長さの制限なし）に切り替えて変換しますか？', '切り替えて変換');
      if (!r) return;
      toNile = true;
    }
    if (c.confirm && c.confirm.length) {
      const list = c.confirm.slice(0, 8).join('\n') + (c.confirm.length > 8 ? `\nほか ${c.confirm.length - 8} 個` : '');
      if (!await this.confirm('上書きの確認', '次のファイルはすでにあります。上書きしますか？\n\n' + list, '上書きする')) return;
    }
    const r = await this.api.start(toNile);
    if (!r.ok) return;
    this.logFrom = 0;
    $('log').textContent = ''; $('log2').textContent = '';
    this.finishedShown = false;
    $('metrics').hidden = true; $('actionbar').hidden = true; $('done').hidden = true; $('progress').hidden = false;
    await this.refresh();
    this.startPolling();
  },

  startPolling() {
    clearInterval(this.timer);
    $('metrics').hidden = true; $('actionbar').hidden = true; $('progress').hidden = false;
    this.timer = setInterval(() => this.poll(), 300);
    this.poll();
  },

  async poll() {
    const p = await this.api.poll(this.logFrom);
    if (p.log && p.log.length) {
      const t = p.log.join('\n') + '\n';
      $('log').textContent += t; $('log2').textContent += t;
      $('log').scrollTop = 1e9;
    }
    this.logFrom = p.log_len;
    for (const it of p.items) {
      const row = document.querySelector(`.row-item[data-id="${it.id}"]`);
      if (!row) continue;
      const [cls, label, ic] = STATUS[it.status] || STATUS.wait;
      if (!row.classList.contains(it.status)) { row.className = `row-item ${it.status}`; row.querySelector('[data-chip]').className = `chip ${cls}`; row.querySelector('[data-chip] use').setAttribute('href', `#i-${ic}`); }
      row.querySelector('[data-chip-text]').textContent = label + (it.status === 'run' ? ` ${Math.round(it.frac * 100)}%` : '');
      row.querySelector('[data-bar]').style.width = (it.status === 'run' ? it.frac * 100 : 0) + '%';
    }
    const j = p.job;
    if (!j) return;
    this.renderStepper(j);
    $('p-title').textContent = j.running ? `変換中：${j.title}` : j.title;
    $('p-count').textContent = j.count > 1 ? `（${j.index} / ${j.count}）` : '';
    $('p-bar').style.width = (j.frac * 100).toFixed(1) + '%';
    $('p-pct').textContent = Math.floor(j.frac * 100) + '%';
    $('p-msg').textContent = j.msg || '';
    $('p-el').textContent = j.elapsed;
    $('p-left').textContent = j.running ? (j.frac > 0.02 || j.index > 1 ? `約 ${j.left}` : '計算中…') : '—';
    if (j.finished && !this.finishedShown) {
      this.finishedShown = true;
      clearInterval(this.timer);
      this.showDone(j);
      this.refresh();
    }
  },

  renderStepper(j) {
    const st = this.info.stages;
    $('stepper').innerHTML = st.map((name, i) => {
      const cls = j.finished && !j.error && !j.cancelled ? 'done' : i < j.stage ? 'done' : i === j.stage ? 'now' : '';
      const ic = cls === 'done' ? 'check-circle' : cls === 'now' ? 'clock' : 'circle';
      return (i ? icon('chev', 'stp-sep') : '') + `<span class="stp ${cls}">${icon(ic)}${esc(name)}</span>`;
    }).join('');
  },

  showDone(j) {
    $('progress').hidden = true;
    $('done').hidden = false;
    const ok = !j.error && !j.cancelled;
    const ic = $('done-ic');
    ic.classList.toggle('bad', !ok);
    ic.querySelector('use').setAttribute('href', ok ? '#i-check-circle' : '#i-alert');
    const n = j.results.length;
    $('done-title').textContent = j.error ? 'エラーで止まりました' : j.cancelled ? '中止しました' : '完了しました';
    $('done-sub').textContent = j.error ? j.error : `${n} 個の ROM を作りました　所要 ${j.elapsed}`;
    const nile = j.results.some((r) => r.wsv);
    $('howto').innerHTML = !n ? '' : nile
      ? `<div class="how"><span class="how-no">1</span><div><b>.WSV</b>（データ）を、microSD の<b>いちばん上のフォルダー（ルート）</b>にコピーします。</div></div>
         <div class="how"><span class="how-no">2</span><div><b>.wsc</b>（ROM）を、microSD の好きなフォルダーにコピーし、nileswan のメニューから選びます。</div></div>`
      : `<div class="how"><span class="how-no">1</span><div><b>.wsc</b> をフラッシュカートに書き込みます。</div></div>
         <div class="how"><span class="how-no">2</span><div>本体の START で一時停止、Y ボタンでジャンプできます。</div></div>`;
    $('results').innerHTML = j.results.map((r) => `<div class="res"><div style="min-width:0"><div class="res-name" title="${esc(r.name)}">${esc(r.name)}${r.wsv ? `　＋　${esc(r.wsv)}（${r.wsv_mb} MB）` : `（ROM ${r.rom_mb} MB）`}</div>
      <div class="res-meta">${esc(r.seconds)}・${r.frames} コマ・${esc(r.fps)} fps・1 コマ平均 ${r.tiles} タイル・変換 ${esc(r.elapsed)}</div>
      ${r.emu ? `<div class="res-meta">エミュレーター用：${esc(r.emu_name)} フォルダー（中の「はじめにお読みください.txt」に見かた）</div>` : ''}
      ${r.emu_error ? `<div class="res-meta">エミュレーター用のイメージは作れませんでした：${esc(r.emu_error)}</div>` : ''}</div>
      <button class="btn small" data-path="${esc(r.out)}">${icon('folder')}場所を開く</button></div>`).join('');
    $('results').onclick = (e) => { const b = e.target.closest('[data-path]'); if (b) this.api.open_file_location(b.dataset.path); };
  },

  // ---- 確認・お知らせ -------------------------------------------------------
  confirm(title, body, okLabel, danger = false) {
    return new Promise((res) => {
      $('modal-title').textContent = title;
      $('modal-body').textContent = body;
      $('modal-actions').innerHTML = `<button class="btn" data-r="0">やめる</button><button class="btn ${danger ? 'danger' : 'primary'}" data-r="1">${esc(okLabel)}</button>`;
      $('modal').hidden = false;
      $('modal-actions').onclick = (e) => { const b = e.target.closest('[data-r]'); if (!b) return; $('modal').hidden = true; res(b.dataset.r === '1'); };
    });
  },

  message(title, body) {
    $('modal-title').textContent = title;
    $('modal-body').textContent = body;
    $('modal-actions').innerHTML = '<button class="btn primary" data-r="1">閉じる</button>';
    $('modal').hidden = false;
    $('modal-actions').onclick = () => { $('modal').hidden = true; };
  },

  toast(msg) {
    const t = $('toast');
    t.textContent = msg;
    t.hidden = false;
    clearTimeout(this._tt);
    this._tt = setTimeout(() => { t.hidden = true; }, 4500);
  },
};

function addTime(start, length) {
  const p = (s) => s ? s.split(':').reduce((a, x) => a * 60 + Number(x), 0) : 0;
  const t = Math.round(p(start) + p(length));
  return `${Math.floor(t / 60)}:${String(t % 60).padStart(2, '0')}`;
}

function waitApi() {
  return new Promise((res) => {
    if (window.pywebview && window.pywebview.api && window.pywebview.api.init) { res(window.pywebview.api); return; }
    let done = false;
    window.addEventListener('pywebviewready', () => { if (!done) { done = true; res(window.pywebview.api); } });
    setTimeout(() => { if (!done) { done = true; res(mockApi()); } }, 1500);
  });
}

// ---- 見本のデータ (ブラウザーで画面だけを確かめるとき) --------------------------
function mockApi() {
  const st = { target: 'nile', k: 3, hz60: true, audio: 1, rom: 16, sd: 350, speaker: 1, quality: 'auto', mode: 'fit', dither: 1.0, bright: 0, contrast: 1, sat: 1.2, norm: true, deint: false, preview: false, emu: false, pal16: true, vmode: 'delta', amode: 'mono' };
  let items = [
    { id: 1, title: '名探偵プリキュア！_S01E01_誕生！名探偵プリキュア！.mp4', desc: 'h264 1920x1080', duration: '24:10', start: '', length: '', trim: '全部', seconds: '24:10', status: 'wait', frac: 0 },
    { id: 2, title: '名探偵プリキュア！_S01E02.mp4', desc: 'h264 1920x1080', duration: '24:10', start: '1:30', length: '22:00', trim: '1:30 〜 23:30', seconds: '22:00', status: 'wait', frac: 0 },
  ];
  let job = null; let t0 = 0;
  const view = () => ({ items, state: st, preset: 'nile60', combine: false, outdir: 'D:\\WSVideo', running: !!(job && !job.finished),
    estimate: { seconds: '46:10', frames: 55690, fps: '20.1', hz: '60Hz', data: { label: 'microSD に置く量', value: '約 690 MB', note: '激しい動画での目安' }, eta: '7 分 10 秒', device: 'NVIDIA GeForce RTX 4090', count: items.length } });
  const id = (x) => Promise.resolve(x);
  return {
    init: () => id({ version: '1.6.0', gpu: 'NVIDIA GeForce RTX 4090', stages: ['動画を読む', '色と動きを調べる', 'データを作る', '書き出す'], view: view(),
      presets: [{ id: 'nile60', title: 'nileswan・60Hz の本体', sub: 'IPS 液晶化・TV スワンなど（20fps）', badge: 'おすすめ' }, { id: 'nile60hq', title: 'nileswan・60Hz・高画質', sub: '30fps・毎コマ描き直し・ラスター（データが多め）' }, { id: 'nile', title: 'nileswan・純正の液晶', sub: '改造していない本体' }, { id: 'rom', title: 'ROM に入れる', sub: 'ほかのフラッシュカート（最大 16MB）' }],
      choices: { fps: [2, 3, 4, 5, 6, 8, 10].map((k) => ({ value: k, label: (12000 / 159 / k).toFixed(1) + ' fps', label60: (12000 / 199 / k).toFixed(1) + ' fps' })),
        sd: [{ value: 350, label: '350 KB/秒（標準・実機で確認済み）' }], rom: [{ value: 16, label: '16 MB' }], quality: [{ value: 'auto', label: '自動' }], pal16: [{ value: true, label: '241色' }],
        dither: [{ value: 1, label: '標準' }], mode: [{ value: 'fit', label: '全体を表示' }], audio: [{ value: 1, label: '12 kHz' }], speaker: [{ value: 1, label: '標準（50%）' }],
        vmode: [{ value: 'delta', label: '差分（標準）' }, { value: 'raw', label: 'パラパラ' }, { value: 'rawpal', label: 'パラパラ＋色' }, { value: 'raster', label: 'パラパラ＋色＋ラスター（最高画質）' }],
        amode: [{ value: 'mono', label: 'モノラル 12kHz（標準）' }, { value: 'hi', label: 'モノラル 24kHz' }, { value: 'stereo', label: 'ステレオ 12kHz' }] } }),
    view: () => id(view()), pick_files: () => id({ view: view(), bad: [] }), clear: () => { items = []; return id(view()); },
    remove: (i) => { items = items.filter((x) => x.id !== i); return id(view()); }, move: () => id(view()),
    set_trim: () => id({ view: view() }), set_preset: () => id(view()), set_state: () => id(view()), reset_state: () => id(view()),
    set_combine: () => id(view()), choose_outdir: () => id(view()), open_folder: () => {}, open_file_location: () => {},
    about: () => id({ app: 'WS Video ROM Maker', version: '1.6.0', gpu: 'NVIDIA GeForce RTX 4090' }),
    check_start: () => id({ ok: true, confirm: [] }),
    start: () => { job = { finished: false }; t0 = Date.now(); items.forEach((x) => { x.status = 'wait'; }); return id({ ok: true }); },
    cancel: () => { if (job) job.cancel = true; },
    poll: () => {
      const el = (Date.now() - t0) / 1000; const frac = Math.min(1, el / 12);
      const cur = frac < 0.5 ? 0 : 1; const f2 = (frac % 0.5) * 2;
      items.forEach((x, i) => { x.status = i < cur || frac >= 1 ? 'done' : i === cur ? 'run' : 'wait'; x.frac = i === cur ? f2 : 1; });
      const fin = frac >= 1 || (job && job.cancel);
      if (fin && job) job.finished = true;
      return id({ items: items.map((x) => ({ id: x.id, status: x.status, frac: x.frac })), log: [], log_len: 0,
        job: { running: !fin, finished: fin, cancelled: !!(job && job.cancel), error: null, frac, stage: f2 < 0.15 ? 0 : f2 < 0.7 ? 1 : f2 < 0.95 ? 2 : 3,
          msg: f2 < 0.7 ? `解析中  ${Math.round(f2 * 29000)}/29000 コマ` : 'データを作成中', index: cur + 1, count: 2, title: items[cur] ? items[cur].title : '', elapsed: Math.round(el) + ' 秒', left: Math.max(0, Math.round(12 - el)) + ' 秒',
          results: fin ? items.map((x) => ({ out: 'D:\\WSVideo\\' + x.title.replace('.mp4', '.wsc'), name: x.title.replace('.mp4', '.wsc'), wsv: 'PRECURE1.WSV', wsv_mb: 372, rom_mb: 1, frames: 29010, seconds: '24:10', fps: '20.1', tiles: 391, elapsed: '3 分 12 秒' })) : [] } });
    },
  };
}

app.boot();
window.app = app;
