/* SPDX-License-Identifier: AGPL-3.0-or-later */
'use strict';
window.addEventListener('DOMContentLoaded', () => {
  const bridge = window.arguments?.[0]?.controller;
  const $ = id => document.getElementById(id);
  if (!bridge) { $('notice').hidden = false; $('notice').textContent = '插件控制器未就绪，请关闭并重新打开此窗口。'; return; }
  const STATUS = { ready: '待开始', submitting: '提交中', unknown: '待确认', submit_failed: '提交失败', queued: '排队中', running: '翻译中', completed: '已完成', failed: '翻译失败', cancelled: '已取消', interrupted: '已中断' };
  const ACTIVE = new Set(['submitting', 'unknown', 'queued', 'running']);
  const fields = ['baseUrl', 'model', 'sourceLang', 'targetLang', 'qps', 'pages', 'dualMode', 'generateMono', 'customPrompt', 'bridgePath'];
  const bindings = [];
  let seenNotice = null;
  let closed = false;
  function on(el, type, fn) { el.addEventListener(type, fn); bindings.push([el, type, fn]); }
  function message(text, error = false) { if (closed) return; $('notice').hidden = !text; $('notice').textContent = text; $('notice').classList.toggle('error', error); }
  async function run(button, action, success) {
    if (button) button.disabled = true;
    try { const result = await action(); if (success) message(typeof success === 'function' ? success(result) : success); return result; }
    catch (error) { message(bridge.safeMessage(error), true); }
    finally { if (button) button.disabled = false; render(); }
  }
  function tab(name) {
    for (const id of ['tasks', 'settings']) {
      $('tab-' + id).classList.toggle('active', name === id);
      $('tab-' + id).setAttribute('aria-selected', String(name === id));
      $(id + '-panel').hidden = name !== id;
    }
  }
  function readSettings() { return Object.fromEntries(fields.map(k => [k, $(k).type === 'checkbox' ? $(k).checked : $(k).value])); }
  function loadSettings() { const c = bridge.getSettings(); for (const k of fields) $(k)[$(k).type === 'checkbox' ? 'checked' : 'value'] = c[k]; keyStatus(); }
  function keyStatus() {
    try { $('key-status').textContent = bridge.hasApiKey($('baseUrl').value) ? '已保存 · 留空保留' : '尚未保存 · 本地模型可留空'; }
    catch (_) { $('key-status').textContent = '凭据库暂不可用'; }
  }
  async function save() {
    const c = readSettings();
    const key = $('apiKey').value;
    await bridge.saveSettings(c, key, key.trim() ? 'replace' : 'keep');
    $('apiKey').value = ''; keyStatus();
  }
  function el(tag, className, text) { const node = document.createElement(tag); if (className) node.className = className; if (text !== undefined) node.textContent = text; return node; }
  function taskButton(parent, text, action) { const b = el('button', 'task-button', text); b.type = 'button'; b.addEventListener('click', () => run(b, action)); parent.appendChild(b); }
  function render() {
    if (closed) return;
    const tasks = bridge.tasks;
    $('task-count').textContent = String(tasks.length);
    $('empty-state').hidden = tasks.length > 0;
    $('start-all').disabled = bridge.starting || !tasks.some(t => t.status === 'ready');
    const running = tasks.filter(t => t.status === 'running').length;
    const queued = tasks.filter(t => ['queued', 'submitting'].includes(t.status)).length;
    const imported = tasks.filter(t => t.importState === 'done').length;
    $('task-summary').textContent = tasks.length ? `${running} 篇翻译中 · ${queued} 篇排队中 · ${imported} 篇已导入` : '可在 Zotero 中多选论文，一次加入队列。';
    if (bridge.notice !== seenNotice) { seenNotice = bridge.notice; if (bridge.notice) message(bridge.notice); }
    if (bridge.health) $('bridge-status').textContent = bridge.health.available
      ? '服务已连接 · 可同时处理 ' + bridge.health.maxConcurrentJobs + ' 篇' : '服务已连接，但翻译引擎尚未安装。请运行安装脚本。';
    const list = $('task-list');
    // Retain native focus while polling so keyboard users do not lose their selected button.
    const focused = document.activeElement;
    const focusTask = focused?.closest?.('[data-task]')?.dataset.task;
    const focusLabel = focused?.textContent;
    list.replaceChildren();
    for (const t of [...tasks].reverse()) {
      const card = el('article', 'task'); card.dataset.task = t.id;
      const top = el('div', 'task-top'); top.appendChild(el('h2', 'task-title', t.title));
      let label = STATUS[t.status] || t.status;
      if (t.status === 'completed') label = t.importState === 'done' ? '已导入' : t.importState === 'error' ? '待重试导入' : '正在导入';
      top.appendChild(el('span', 'badge' + (['failed', 'submit_failed', 'interrupted'].includes(t.status) || t.importState === 'error' ? ' bad' : t.importState === 'done' ? ' done' : ''), label));
      card.appendChild(top);
      const setting = t.config ? `${t.config.sourceLang} → ${t.config.targetLang} · ${t.config.model}${t.config.pages ? ' · 第 ' + t.config.pages + ' 页' : ''}` : '使用当前设置 · 尚未提交论文';
      card.appendChild(el('p', 'task-stage', setting + (t.stage ? '  /  ' + t.stage : '')));
      if (ACTIVE.has(t.status)) {
        const progress = el('progress'); progress.max = 100;
        if (Number.isFinite(t.progress)) progress.value = t.progress;
        progress.setAttribute('aria-label', '翻译进度'); card.appendChild(progress);
      }
      if (t.error || t.importError) card.appendChild(el('p', 'task-error', t.importError || t.error));
      const actions = el('div', 'task-actions');
      if (t.status === 'ready') taskButton(actions, '开始', () => bridge.startTask(t.id));
      if (['unknown', 'submit_failed'].includes(t.status)) taskButton(actions, '重新提交', () => bridge.startTask(t.id));
      if (ACTIVE.has(t.status) || t.status === 'ready') taskButton(actions, '取消', () => bridge.cancelTask(t.id));
      if (t.status === 'completed' && t.importState === 'error') taskButton(actions, '重试导入', () => bridge.importTask(t.id));
      if (t.imported?.dual) taskButton(actions, '打开双语 PDF', () => bridge.openResult(t.id));
      if (!ACTIVE.has(t.status)) taskButton(actions, '移除记录', () => bridge.removeTask(t.id));
      card.appendChild(actions); list.appendChild(card);
    }
    if (focusTask) {
      const card = [...list.children].find(c => c.dataset.task === focusTask);
      const button = card && [...card.querySelectorAll('button')].find(b => b.textContent === focusLabel);
      button?.focus({ preventScroll: true });
    }
  }
  on($('tab-tasks'), 'click', () => tab('tasks'));
  on($('tab-settings'), 'click', () => tab('settings'));
  on($('empty-settings'), 'click', () => tab('settings'));
  on($('baseUrl'), 'input', keyStatus);
  on($('settings-form'), 'submit', e => { e.preventDefault(); run($('save-settings'), save, '设置已保存。'); });
  on($('clear-key'), 'click', () => run($('clear-key'), async () => {
    const c = readSettings();
    await bridge.saveSettings(c, '', 'clear'); $('apiKey').value = ''; keyStatus();
  }, '已清除此 API 地址保存的密钥。'));
  on($('test-api'), 'click', () => run($('test-api'), async () => {
    $('api-status').textContent = '正在发送一句测试文本…'; await save();
    try { await bridge.testConnection(); $('api-status').textContent = 'API 连接成功，可开始试译。'; }
    catch (e) { $('api-status').textContent = 'API 测试失败'; throw e; }
  }, 'API 测试成功。建议先翻译 1–2 页。'));
  on($('check-bridge'), 'click', () => run($('check-bridge'), async () => {
    $('bridge-status').textContent = '正在检查…'; await save();
    try { return await bridge.checkConnection(); }
    catch (e) { $('bridge-status').textContent = '本地服务未连接'; throw e; }
  }, h => h.available ? '本地服务和翻译引擎已就绪。' : '本地服务已连接；请先安装翻译引擎。'));
  on($('add-selected'), 'click', () => run($('add-selected'), () => bridge.queueSelection()));
  on($('start-all'), 'click', () => run($('start-all'), async () => { await save(); await bridge.startReady(); }));
  on($('refresh'), 'click', () => run($('refresh'), () => bridge.refreshJobs()));
  on(document, 'visibilitychange', () => bridge.schedule());
  const unsubscribe = bridge.subscribe(render);
  on(window, 'unload', () => {
    closed = true; unsubscribe();
    for (const [node, type, fn] of bindings) node.removeEventListener(type, fn);
    $('apiKey').value = ''; bridge.windowClosed(window);
  });
  loadSettings(); render(); bridge.windowReady(window);
});
