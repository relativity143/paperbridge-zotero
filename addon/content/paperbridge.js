/* SPDX-License-Identifier: AGPL-3.0-or-later */
/* Native Zotero controller. The dependency object also permits isolated VM tests. */
(function (global) {
  'use strict';
  const PREF = 'extensions.paperbridge.';
  const TAG = 'PaperBridge 纸桥译文';
  const LEGACY_RELATION = /^urn:paperbridge:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}:(dual|mono)$/i;
  const ACTIVE = new Set(['submitting', 'unknown', 'queued', 'running']);
  const REMOTE_STATES = new Set(['queued', 'running', 'completed', 'failed', 'cancelled', 'interrupted']);
  const DEFAULTS = Object.freeze({ baseUrl: 'https://api.openai.com/v1', model: 'gpt-4.1-mini',
    sourceLang: 'en', targetLang: 'zh', qps: 1, pages: '', dualMode: 'side-by-side', generateMono: false, customPrompt: '', bridgePath: '' });
  class PaperBridgeController {
    constructor(env) {
      this.env = env;
      this.Zotero = env.Zotero;
      this.Services = env.Services;
      this.tasks = [];
      this.windows = new Map();
      this.listeners = new Set();
      this.secrets = new Set();
      this.busy = new Set();
      this.enabled = true;
      this.uiWindow = null;
      this.timer = null;
      this.polling = false;
      this.starting = false;
      this.failures = 0;
      this.notice = '';
      this.health = null;
      this.renameTimer = null;
      this.renaming = new Set();
      this.changedAttachments = new Set();
    }
    prefGet(key, fallback) {
      try { const v = this.Zotero.Prefs.get(PREF + key, true); return v === undefined ? fallback : v; }
      catch (_) { return fallback; }
    }
    prefSet(key, value) { this.Zotero.Prefs.set(PREF + key, value, true); }
    init() {
      try {
        const rows = JSON.parse(this.prefGet('tasks', '[]'));
        this.tasks = Array.isArray(rows) ? rows.filter(t => t && /^[\w-]{16,80}$/.test(t.id)
          && Number.isInteger(t.sourceId)).map(t => this.taskRecord(t)) : [];
      } catch (_) { this.notice = '任务记录无法读取。原始 PDF 不受影响。'; }
      for (const t of this.tasks) {
        if (t.status === 'submitting') t.status = 'unknown';
        if (t.importState === 'importing') t.importState = 'pending';
      }
      // Resume saved work with Zotero, even if the translation window is closed.
      this.schedule();
    }
    async repairLegacyRelations() {
      if (this.prefGet('relationRepairVersion', 0) >= 1) return { items: 0, relations: 0 };
      const ids = await this.Zotero.DB.columnQueryAsync(
        'SELECT DISTINCT itemID FROM itemRelations JOIN relationPredicates USING (predicateID) WHERE predicate=? AND object LIKE ?',
        ['dc:relation', 'urn:paperbridge:%']);
      const affected = [];
      for (const id of ids) {
        const item = await this.Zotero.Items.getAsync(id);
        if (!item) continue;
        const values = (item.getRelations()['dc:relation'] || []).filter(value => LEGACY_RELATION.test(value));
        if (values.length) affected.push({ item, values });
      }
      const result = { items: affected.length, relations: affected.reduce((n, entry) => n + entry.values.length, 0) };
      if (affected.length) {
        const profile = this.Services.dirsvc.get('ProfD', this.env.Components.interfaces.nsIFile).path;
        const directory = this.env.PathUtils.join(profile, 'paperbridge-repair-backups');
        await this.env.IOUtils.makeDirectory(directory, { ignoreExisting: true, permissions: 0o700 });
        result.backupPath = this.env.PathUtils.join(directory, 'relations-' + this.env.crypto.randomUUID() + '.json');
        // Never change library records unless a recoverable, narrowly scoped backup was saved first.
        await this.env.IOUtils.writeJSON(result.backupPath, { schema: 1, createdAt: new Date().toISOString(),
          entries: affected.map(({ item, values }) => ({ itemID: item.id, key: item.key,
            libraryID: item.libraryID, predicate: 'dc:relation', values })) });
        await this.env.IOUtils.setPermissions(result.backupPath, 0o600);
        for (const { item, values } of affected) {
          for (const value of values) item.removeRelation('dc:relation', value);
          await item.saveTx({ skipDateModifiedUpdate: true });
        }
        this.notice = `已修复 ${result.items} 个译文附件的旧版关联标记，PDF 和正常文献关联已保留。请重新同步 Zotero。`;
      }
      this.prefSet('relationRepairReport', JSON.stringify(result));
      this.prefSet('relationRepairVersion', 1);
      this.emit();
      return result;
    }
    taskRecord(t) {
      const out = {};
      for (const key of ['id', 'sourceId', 'parentId', 'libraryId', 'collections', 'inputPath', 'title',
        'status', 'progress', 'stage', 'createdAt', 'submittedAt', 'error', 'importError',
        'importState', 'imported', 'opened', 'outputs', 'sourceMetadata', 'naming', 'namedFiles']) {
        if (t[key] !== undefined) out[key] = t[key];
      }
      if (t.config) out.config = this.cleanSettings(t.config);
      return out;
    }
    persist() { if (this.enabled) this.prefSet('tasks', JSON.stringify(this.tasks.map(t => this.taskRecord(t)))); }
    cleanSettings(raw = {}) {
      const out = {};
      for (const k of Object.keys(DEFAULTS)) out[k] = raw[k] === undefined ? DEFAULTS[k] : raw[k];
      return out;
    }
    getSettings() {
      try { return this.cleanSettings(JSON.parse(this.prefGet('settings', '{}'))); }
      catch (_) { return { ...DEFAULTS }; }
    }
    isLoopback(host) { return ['127.0.0.1', 'localhost', '[::1]', '::1'].includes(host.toLowerCase()); }
    validateSettings(raw) {
      const c = this.cleanSettings(raw);
      for (const k of ['baseUrl', 'model', 'sourceLang', 'targetLang', 'pages', 'customPrompt', 'bridgePath']) {
        if (typeof c[k] !== 'string') throw new Error('设置字段必须为文本。');
        c[k] = c[k].trim();
      }
      let u;
      try { u = new this.env.URL(c.baseUrl); } catch (_) { throw new Error('API 地址格式无效。'); }
      if (!['https:', 'http:'].includes(u.protocol) || u.username || u.password || u.search || u.hash)
        throw new Error('API 地址须为不含密钥、查询参数或片段的 HTTP(S) 地址。');
      if (u.protocol === 'http:' && !this.isLoopback(u.hostname))
        throw new Error('远程 API 请使用 HTTPS；HTTP 仅支持本机服务。');
      c.baseUrl = c.baseUrl.replace(/\/+$/, '');
      if (!c.model || c.model.length > 200) throw new Error('请输入模型名称，最多 200 个字符。');
      for (const k of ['sourceLang', 'targetLang']) {
        if (!/^[a-zA-Z][a-zA-Z0-9_-]{0,23}$/.test(c[k])) throw new Error('语言请填写代码，例如 en、zh 或 zh-TW。');
      }
      c.qps = Number(c.qps);
      if (!Number.isInteger(c.qps) || c.qps < 1 || c.qps > 10) throw new Error('请求速率须为 1–10 QPS 的整数。');
      if (c.pages && !/^\d+(?:-\d+)?(?:\s*,\s*\d+(?:-\d+)?)*$/.test(c.pages))
        throw new Error('页码格式例如 1-3,5；留空表示全文。');
      for (const range of c.pages.split(',').filter(Boolean)) {
        const [a, b] = range.trim().split('-').map(Number);
        if (a < 1 || a > 100000 || (b !== undefined && (b < a || b > 100000))) throw new Error('页码必须为正数，且范围从小到大。');
      }
      c.pages = c.pages.replace(/\s/g, '');
      if (!['side-by-side', 'alternating'].includes(c.dualMode)) throw new Error('请选择有效的双语排版。');
      if (typeof c.generateMono !== 'boolean') throw new Error('请选择是否额外生成单语译文 PDF。');
      if (c.customPrompt.length > 12000) throw new Error('补充提示词最多 12,000 个字符。');
      if (c.bridgePath && !this.isAbsolute(c.bridgePath)) throw new Error('配对文件须填写绝对路径。');
      return c;
    }
    logins(baseUrl) { return this.Services.logins.findLogins('chrome://paperbridge', null, baseUrl); }
    hasApiKey(baseUrl) { return this.logins(baseUrl.replace(/\/+$/, '')).length > 0; }
    getApiKey(baseUrl) {
      const secret = this.logins(baseUrl)[0]?.password || '';
      if (secret) this.secrets.add(secret);
      return secret;
    }
    async saveSettings(raw, apiKey = '', keyAction = 'keep') {
      const c = this.validateSettings(raw);
      if (!['keep', 'replace', 'clear'].includes(keyAction)) throw new Error('密钥操作无效。');
      if (keyAction === 'replace' && !apiKey.trim()) throw new Error('新密钥不能为空；本机服务可使用“清除密钥”。');
      if (keyAction !== 'keep') {
        const old = this.logins(c.baseUrl);
        if (keyAction === 'clear') {
          for (const login of old) this.Services.logins.removeLogin(login);
        } else {
          const key = apiKey.trim();
          this.secrets.add(key);
          const login = this.env.Components.classes['@mozilla.org/login-manager/loginInfo;1']
            .createInstance(this.env.Components.interfaces.nsILoginInfo);
          login.init('chrome://paperbridge', null, c.baseUrl, 'api-key', key, '', '');
          if (old.length) this.Services.logins.modifyLogin(old[0], login);
          else await this.Services.logins.addLoginAsync(login);
          for (const duplicate of old.slice(1)) this.Services.logins.removeLogin(duplicate);
        }
      }
      if (!this.enabled) return c;
      this.prefSet('settings', JSON.stringify(c));
      this.emit();
      return c;
    }
    safeMessage(error) {
      let s = typeof error === 'string' ? error : error?.message || '发生未知错误。';
      for (const secret of this.secrets) if (secret) s = s.split(secret).join('[已隐藏]');
      return s.replace(/Bearer\s+\S+/gi, 'Bearer [已隐藏]')
        .replace(/\bsk-[\w-]+/g, '[已隐藏]').slice(0, 1200);
    }
    isAbsolute(path) { return typeof path === 'string' && (/^\//.test(path) || /^[a-zA-Z]:[\\/]/.test(path)); }
    defaultBridgePath() {
      const home = this.Services.dirsvc.get('Home', this.env.Components.interfaces.nsIFile).path;
      return this.env.PathUtils.join(home, 'Library', 'Application Support', 'PaperBridge', 'bridge.json');
    }
    async readBridge() {
      const path = this.getSettings().bridgePath || this.defaultBridgePath();
      let data;
      try { data = await this.env.IOUtils.readJSON(path); }
      catch (_) { throw new Error('未找到本地翻译服务。请先运行启动器，或在设置中指定 bridge.json 的绝对路径。'); }
      let url;
      try { url = new this.env.URL(data.url); } catch (_) { throw new Error('本地配对文件的地址无效。'); }
      if (!this.isLoopback(url.hostname) || !['http:', 'https:'].includes(url.protocol)
        || url.username || url.password || url.search || url.hash || url.pathname !== '/')
        throw new Error('配对地址只允许本机回环 HTTP(S) 服务，且不得包含路径或凭据。');
      if (typeof data.token !== 'string' || data.token.length < 16 || /\s/.test(data.token))
        throw new Error('配对令牌无效，请重新运行本地启动器。');
      this.secrets.add(data.token);
      return { url: url.origin, token: data.token };
    }
    async request(method, path, body, timeout = 15000) {
      if (!this.enabled) throw new Error('插件已停用。');
      const bridge = await this.readBridge();
      let response;
      try {
        response = await this.Zotero.HTTP.request(method, bridge.url + path, {
          headers: { Authorization: 'Bearer ' + bridge.token, 'Content-Type': 'application/json' },
          ...(body ? { body: JSON.stringify(body) } : {}), responseType: 'json', timeout,
          logBodyLength: 0, debug: false, anon: true, followRedirects: false,
          errorDelayIntervals: [], successCodes: [200, 201, 202, 400, 401, 403, 404, 409, 422, 500, 503]
        });
      } catch (_) { throw new Error('无法连接本地翻译服务，或请求超时。请确认启动器仍在运行。'); }
      if (!this.enabled) throw new Error('插件已停用。');
      let data = response.response;
      if (typeof data === 'string') { try { data = JSON.parse(data); } catch (_) { data = null; } }
      if (!data || typeof data !== 'object') throw new Error('本地服务返回了无效响应。');
      if (response.status >= 300) {
        const detail = typeof data.error === 'string' ? data.error : data.error?.message || data.detail;
        const error = new Error(this.safeMessage(typeof detail === 'string' ? detail : '本地服务请求失败（' + response.status + '）。'));
        error.status = response.status;
        throw error;
      }
      return data;
    }
    async checkConnection() {
      const h = await this.request('GET', '/v1/health');
      if (h.ok !== true) throw new Error('本地服务健康检查未通过。');
      this.health = { available: !!h.engine?.available, version: h.engine?.version || '',
        maxConcurrentJobs: Number.isInteger(h.maxConcurrentJobs) ? h.maxConcurrentJobs : 1 };
      this.emit();
      return this.health;
    }
    async testConnection() {
      const c = this.validateSettings(this.getSettings());
      const apiKey = this.getApiKey(c.baseUrl);
      const result = await this.request('POST', '/v1/test', { config: { ...this.apiConfig(c), apiKey } }, 60000);
      if (result.ok !== true) throw new Error(this.safeMessage(result.error || 'API 测试失败。'));
      return result;
    }
    apiConfig(c) { const out = { ...c }; delete out.bridgePath; return out; }
    isOwnOutput(item) {
      return item.hasTag?.(TAG) || /^paperbridge-[\w-]+-(dual|mono)\.pdf$/i.test(item.attachmentFilename || '')
        || (item.getRelations?.()['dc:relation'] || []).some(v => v.startsWith('urn:paperbridge:'));
    }
    async queueItems(items) {
      const attachments = new Map();
      for (const item of items) {
        if (!item || item.deleted) continue;
        if (item.isAttachment()) attachments.set(item.id, item);
        else if (item.isRegularItem()) {
          for (const id of item.getAttachments()) {
            const child = await this.Zotero.Items.getAsync(id);
            if (child) attachments.set(id, child);
          }
        }
      }
      let added = 0;
      let skipped = 0;
      for (const item of attachments.values()) {
        if (item.deleted || item.attachmentContentType !== 'application/pdf' || this.isOwnOutput(item)) { skipped++; continue; }
        if (this.tasks.some(t => t.sourceId === item.id && (ACTIVE.has(t.status) || t.status === 'ready'))) { skipped++; continue; }
        const path = await item.getFilePathAsync();
        if (!path || !await this.env.IOUtils.exists(path)) { skipped++; continue; }
        this.tasks.push({ id: this.env.crypto.randomUUID(), sourceId: item.id, parentId: item.parentItemID || null,
          libraryId: item.libraryID, collections: item.parentItemID ? [] : item.getCollections(), inputPath: path,
          sourceMetadata: await this.readNamingMetadata(item),
          title: item.getField('title') || item.attachmentFilename || 'PDF', status: 'ready', progress: null,
          stage: '等待确认', createdAt: new Date().toISOString(), imported: {}, importState: 'pending' });
        added++;
      }
      this.notice = added ? `已加入 ${added} 份 PDF。点击“开始翻译”后才会提交给所选 API。${skipped ? ` 跳过 ${skipped} 份非 PDF、译文、未下载或重复附件。` : ''}`
        : '没有可加入的本地 PDF。请选择条目或已下载的 PDF 附件；译文和队列中的附件会自动跳过。';
      this.persist(); this.emit();
      return added;
    }
    async queueSelection(win = this.Zotero.getMainWindow()) { return this.queueItems(win?.ZoteroPane?.getSelectedItems() || []); }
    async translateSelection(win) { this.openWindow(); return this.queueSelection(win); }
    task(id) { const t = this.tasks.find(t => t.id === id); if (!t) throw new Error('任务不存在。'); return t; }
    async startTask(id) {
      const t = this.task(id);
      if (this.busy.has(id) || !['ready', 'unknown', 'submit_failed'].includes(t.status)) return;
      this.busy.add(id);
      try {
        // Keep the same request ID/config when a previous POST has an uncertain outcome.
        const c = t.config ? this.validateSettings(t.config) : this.validateSettings(this.getSettings());
        const apiKey = this.getApiKey(c.baseUrl);
        if (!await this.env.IOUtils.exists(t.inputPath)) throw new Error('原始 PDF 已移动或未下载，请先恢复附件。');
        t.config = c; t.status = 'submitting'; t.error = ''; t.stage = '正在提交';
        t.submittedAt = t.submittedAt || new Date().toISOString();
        this.persist(); this.emit();
        try {
          const job = await this.request('POST', '/v1/jobs', { id: t.id, inputPath: t.inputPath,
            config: { ...this.apiConfig(c), apiKey } }, 30000);
          this.applyJob(t, job);
        } catch (e) {
          if (!this.enabled) return;
          t.status = e.status && e.status < 500 ? 'submit_failed' : 'unknown';
          t.error = this.safeMessage(e); t.stage = t.status === 'unknown' ? '提交结果待确认' : '提交失败';
        }
      } catch (e) { t.status = 'submit_failed'; t.error = this.safeMessage(e); }
      finally { this.busy.delete(id); this.persist(); this.emit(); this.schedule(); }
    }
    async startReady() {
      if (this.starting) return;
      const ids = this.tasks.filter(t => t.status === 'ready').map(t => t.id);
      this.starting = true; this.emit();
      try {
        await Promise.all(Array.from({ length: Math.min(2, ids.length) }, async () => {
          while (this.enabled && ids.length) await this.startTask(ids.shift());
        }));
      } finally { this.starting = false; this.emit(); }
    }
    applyJob(t, job) {
      if (!job || job.id !== t.id || !REMOTE_STATES.has(job.status)) throw new Error('服务返回的任务信息不匹配。');
      t.status = job.status;
      t.progress = Number.isFinite(job.progress) ? Math.max(0, Math.min(100, job.progress)) : null;
      t.stage = this.safeMessage(typeof job.stage === 'string' ? job.stage : '');
      t.error = job.error ? this.safeMessage(typeof job.error === 'string' ? job.error : '翻译失败，请检查本地服务。') : '';
      if (job.outputs) {
        t.outputs = {};
        for (const kind of ['dual', 'mono']) {
          const p = job.outputs[kind];
          if (this.isAbsolute(p) && /\.pdf$/i.test(p) && p !== t.inputPath) t.outputs[kind] = p;
        }
      }
    }
    async refreshJobs() {
      if (!this.enabled || this.polling) return;
      this.polling = true;
      let failed = false;
      try {
        const pending = this.tasks.filter(t => ACTIVE.has(t.status) && !this.busy.has(t.id));
        await Promise.all(Array.from({ length: Math.min(4, pending.length) }, async () => {
          while (this.enabled && pending.length) {
            const t = pending.shift();
            try { this.applyJob(t, await this.request('GET', '/v1/jobs/' + encodeURIComponent(t.id))); }
            catch (e) {
              if (!this.enabled) return;
              if (e.status === 404) { t.status = 'submit_failed'; t.error = '本地服务中没有此任务。可点击“重新提交”使用原任务编号安全重试。'; }
              else { t.error = this.safeMessage(e); failed = true; }
            }
          }
        }));
        for (const t of this.tasks.filter(t => t.status === 'completed' && t.importState === 'pending')) {
          if (!this.enabled) break;
          await this.importTask(t.id);
        }
        this.failures = failed ? this.failures + 1 : 0;
        this.persist(); this.emit();
      } finally { this.polling = false; this.schedule(); }
    }
    visible() { return this.uiWindow && !this.uiWindow.closed && !this.uiWindow.document.hidden; }
    schedule() {
      if (this.timer) { this.env.clearTimeout(this.timer); this.timer = null; }
      if (!this.enabled || !this.tasks.some(t => ACTIVE.has(t.status)
        || (t.status === 'completed' && t.importState === 'pending'))) return;
      this.timer = this.env.setTimeout(() => { this.timer = null; return this.refreshJobs().catch(e => this.report(e)); },
        Math.min(30000, 2000 * 2 ** Math.min(this.failures, 4)));
    }
    async cancelTask(id) {
      const t = this.task(id);
      if (t.status === 'ready' || t.status === 'submit_failed') { t.status = 'cancelled'; t.stage = '已取消'; }
      else if (ACTIVE.has(t.status)) {
        if (this.busy.has(id)) throw new Error('任务正在提交，请稍后取消。');
        this.applyJob(t, await this.request('POST', '/v1/jobs/' + encodeURIComponent(id) + '/cancel', {}));
      }
      this.persist(); this.emit(); this.schedule();
    }
    async findImported(t, kind) {
      const saved = t.imported?.[kind] && await this.Zotero.Items.getAsync(t.imported[kind]);
      if (saved && !saved.deleted) return saved;
      // A file organizer may replace a stored attachment with a linked clone.
      // Tags survive that replacement; filenames and item IDs do not.
      const tagged = await this.Zotero.DB.columnQueryAsync(
        'SELECT itemID FROM itemTags JOIN tags USING (tagID) WHERE name=?', [this.outputTag(t, kind)]);
      for (const id of tagged) {
        const item = await this.Zotero.Items.getAsync(id);
        if (item && !item.deleted && item.libraryID === t.libraryId && (item.parentItemID || null) === t.parentId) return item;
      }
      // importFromFile commits the attachment before returning. The deterministic storage filename
      // is a recovery marker even if Zotero stops before we save the tag or preference record.
      const ids = await this.Zotero.DB.columnQueryAsync('SELECT itemID FROM itemAttachments WHERE path=? OR path LIKE ?',
        [`storage:paperbridge-${t.id}-${kind}.pdf`, `%/paperbridge-${t.id}-${kind}.pdf`]);
      for (const id of ids) {
        const item = await this.Zotero.Items.getAsync(id);
        if (item && !item.deleted && item.libraryID === t.libraryId && (item.parentItemID || null) === t.parentId) return item;
      }
      return null;
    }
    outputTag(t, kind) { return `PaperBridge:${t.id}:${kind}`; }
    cleanName(value) {
      return String(value || '').replace(/<[^>]*>/g, '').replace(/\.pdf$/i, '')
        .replace(/[\x00-\x1f\x7f/\\:*?"<>|]/g, ' ').replace(/\s+/g, ' ').trim();
    }
    clipName(value, maxBytes) {
      let result = '', bytes = 0;
      for (const char of value) {
        const n = char.codePointAt(0), size = n < 0x80 ? 1 : n < 0x800 ? 2 : n < 0x10000 ? 3 : 4;
        if (bytes + size > maxBytes) break;
        result += char; bytes += size;
      }
      return result.trim();
    }
    async readNamingMetadata(source) {
      const item = source?.parentItemID ? await this.Zotero.Items.getAsync(source.parentItemID) : source;
      const field = key => { try { return item?.getField(key) || ''; } catch (_) { return ''; } };
      const extra = field('extra'), han = /[\u3400-\u9fff]/;
      const alternate = extra.split(/\r?\n/).map(line => line.match(/^\s*(?:original[ -]?title|english[ -]?title|translated[ -]?title|title[ -]?translation|中文标题|英文标题|原文标题|译名)\s*:\s*(.+)$/i)?.[1]).filter(Boolean);
      const chinese = han.test(field('publicationTitle')) || /^(?:zh(?:[-_].*)?|chi|zho|Chinese|中文)$/i.test(field('language').trim());
      const candidates = [field('title'), ...alternate].filter(Boolean);
      const title = candidates.find(title => chinese ? han.test(title) : !han.test(title)) || candidates[0] || '';
      const allCreators = item?.getCreators?.() || [];
      const authorID = this.Zotero.CreatorTypes?.getID('author');
      const authors = allCreators.filter(c => c.creatorType === 'author' || (authorID && c.creatorTypeID === authorID));
      const creators = authors.length ? authors : allCreators;
      const first = creators[0];
      const author = first ? (han.test(first.lastName || '') && !first.fieldMode
        ? (first.lastName || '') + (first.firstName || '') : first.lastName || first.name || first.firstName || '') : '';
      const year = (field('date').match(/\b(?:1[5-9]|20|21)\d{2}\b/) || [])[0] || '';
      return { author: author ? author + (creators.length > 1 ? (chinese ? ' 等' : ' et al.') : '') : '', year, title, chinese };
    }
    async prepareNaming(t) {
      if (t.naming?.base) return t.naming;
      const source = await this.Zotero.Items.getAsync(t.sourceId);
      const live = source && !source.deleted ? await this.readNamingMetadata(source) : {};
      const meta = { ...t.sourceMetadata };
      for (const [key, value] of Object.entries(live)) if (value) meta[key] = value;
      const author = this.clipName(this.cleanName(meta.author || 'Unknown author'), 65);
      const year = meta.year || 'n.d.';
      const title = this.cleanName(meta.title || t.title || 'Untitled');
      t.naming = { base: this.clipName(`${author} - ${year} - ${title}`, 215), language: meta.chinese ? 'zh' : 'en' };
      this.persist();
      return t.naming;
    }
    async nameImported(t, kind, attachment) {
      if (this.renaming.has(attachment.id)) return;
      this.renaming.add(attachment.id);
      try {
        const { base } = await this.prepareNaming(t);
        const title = base + (kind === 'mono' ? ' (translation)' : '');
        const filename = title + '.pdf';
        // Remember Zotero's actual collision-safe name to avoid renaming it again.
        if (attachment.attachmentFilename !== filename && attachment.attachmentFilename !== t.namedFiles?.[kind]) {
          const result = await attachment.renameAttachmentFile(filename, { overwrite: false, unique: true });
          if (result !== true) throw new Error('译文已保存，但文件重命名失败。请检查附件文件是否可写。');
        }
        t.namedFiles = t.namedFiles || {}; t.namedFiles[kind] = attachment.attachmentFilename;
        t.imported = t.imported || {}; t.imported[kind] = attachment.id;
        this.persist();
        if (attachment.getField('title') !== title) { attachment.setField('title', title); await attachment.saveTx(); }
      } finally { this.renaming.delete(attachment.id); }
    }
    watchNamedAttachments() {
      if (this.renameObserver || !this.Zotero.Notifier) return;
      this.renameObserver = this.Zotero.Notifier.registerObserver({ notify: (event, type, ids) => {
        if (!this.enabled || !['add', 'modify'].includes(event)) return;
        for (const id of ids) if (!this.renaming.has(id)) this.changedAttachments.add(id);
        if (this.renameTimer) this.env.clearTimeout(this.renameTimer);
        this.renameTimer = this.env.setTimeout(() => {
          this.renameTimer = null;
          const changed = [...this.changedAttachments]; this.changedAttachments.clear();
          this.reconcileNamedAttachments(changed).catch(e => this.report(e));
        }, 1000);
      } }, ['item'], 'paperbridge-naming');
    }
    async reconcileNamedAttachments(ids) {
      for (const id of ids) {
        if (!this.enabled) return;
        const item = await this.Zotero.Items.getAsync(id);
        if (!item || item.deleted || !item.isAttachment() || !item.hasTag(TAG)) continue;
        for (const t of this.tasks.filter(t => t.naming?.base && t.libraryId === item.libraryID && t.parentId === (item.parentItemID || null))) {
          for (const kind of ['dual', 'mono']) if (item.hasTag(this.outputTag(t, kind))) await this.nameImported(t, kind, item);
        }
      }
    }
    async importTask(id) {
      const t = this.task(id);
      if (this.busy.has(id) || t.status !== 'completed' || !this.enabled) return;
      this.busy.add(id);
      t.importState = 'importing'; t.importError = ''; this.persist(); this.emit();
      try {
        const library = this.Zotero.Libraries.get(t.libraryId);
        if (!library || !library.editable || library.filesEditable === false) throw new Error('目标文献库无法写入附件，请确认库权限后重试导入。');
        if (t.parentId) {
          const parent = await this.Zotero.Items.getAsync(t.parentId);
          if (!parent || parent.deleted || parent.libraryID !== t.libraryId) throw new Error('原父条目已删除或移动，请恢复条目后重试导入。');
        }
        // Older completed jobs can already contain both outputs; keep importing
        // those. New jobs only require mono when it was explicitly requested.
        for (const kind of ['dual', ...(t.outputs?.mono || t.config?.generateMono ? ['mono'] : [])]) {
          if (!this.enabled) return;
          let attachment = await this.findImported(t, kind);
          if (!attachment) {
            const path = t.outputs?.[kind];
            if (!path || !await this.env.IOUtils.exists(path)) throw new Error(`找不到${kind === 'dual' ? '双语' : '单语'} PDF 输出。请确认本地任务目录仍在。`);
            const bytes = await this.env.IOUtils.read(path, { maxBytes: 5 });
            if (Array.from(bytes).map(n => String.fromCharCode(n)).join('') !== '%PDF-') throw new Error('输出文件不是有效的 PDF。');
            attachment = await this.Zotero.Attachments.importFromFile({ file: path, libraryID: t.libraryId,
              parentItemID: t.parentId || undefined, collections: t.parentId ? undefined : t.collections,
              title: `纸桥 · ${kind === 'dual' ? '双语对照' : '单语译文'} · ${t.title}`,
              fileBaseName: `paperbridge-${t.id}-${kind}`, contentType: 'application/pdf' });
          }
          t.imported = t.imported || {}; t.imported[kind] = attachment.id;
          this.persist();
          attachment.addTag(TAG);
          attachment.addTag(this.outputTag(t, kind), 1);
          await attachment.saveTx();
          await this.nameImported(t, kind, attachment);
        }
        t.importState = 'done'; this.persist();
        // Silent background import must not steal focus or open reader tabs.
        if (!t.opened && this.enabled && this.visible() && this.uiWindow.document.hasFocus?.()) {
          await this.openResult(id); t.opened = true;
        }
      } catch (e) { t.importState = 'error'; t.importError = this.safeMessage(e); }
      finally { this.busy.delete(id); this.persist(); this.emit(); }
    }
    async openResult(id) {
      const t = this.task(id);
      const item = await this.findImported(t, 'dual');
      if (!item) throw new Error('双语 PDF 尚未导入，请先重试导入。');
      await this.Zotero.Reader.open(item.id);
    }
    removeTask(id) {
      const t = this.task(id);
      if (ACTIVE.has(t.status) || this.busy.has(id)) throw new Error('请先取消运行中的任务。');
      this.tasks = this.tasks.filter(t => t.id !== id); this.persist(); this.emit();
    }
    subscribe(fn) { this.listeners.add(fn); return () => this.listeners.delete(fn); }
    emit() { if (this.enabled) for (const fn of this.listeners) { try { fn(); } catch (_) {} } }
    report(e) { this.notice = this.safeMessage(e); this.emit(); }
    addWindow(win) {
      if (this.windows.has(win) || !this.enabled) return;
      const doc = win.document, nodes = [], handlers = [];
      const append = (parentId, id, label, handler) => {
        const parent = doc.getElementById(parentId); if (!parent) return;
        const menu = doc.createXULElement('menuitem');
        menu.id = id; menu.setAttribute('label', label); menu.setAttribute('class', 'menuitem-iconic');
        menu.setAttribute('image', 'chrome://paperbridge/content/icon.svg');
        menu.addEventListener('command', handler); handlers.push([menu, 'command', handler]);
        parent.appendChild(menu); nodes.push(menu);
      };
      append('menu_ToolsPopup', 'paperbridge-tools', '纸桥翻译：任务与设置', () => this.openWindow());
      append('zotero-itemmenu', 'paperbridge-translate', '纸桥翻译：双语 PDF', () => this.translateSelection(win).catch(e => this.report(e)));
      this.windows.set(win, { nodes, handlers });
    }
    removeWindow(win) {
      const record = this.windows.get(win); if (!record) return;
      for (const [el, event, handler] of record.handlers) el.removeEventListener(event, handler);
      for (const el of record.nodes) el.remove();
      this.windows.delete(win);
    }
    openWindow() {
      if (this.uiWindow && !this.uiWindow.closed) { this.uiWindow.focus(); return this.uiWindow; }
      this.uiWindow = this.Zotero.getMainWindow().openDialog('chrome://paperbridge/content/window.xhtml',
        'paperbridge', 'chrome,centerscreen,resizable,dialog=no,width=1080,height=790', { controller: this });
      return this.uiWindow;
    }
    windowReady(win) { this.uiWindow = win; this.refreshJobs().catch(e => this.report(e)); }
    windowClosed(win) { if (this.uiWindow === win) this.uiWindow = null; this.schedule(); }
    shutdown() {
      this.enabled = false;
      if (this.renameTimer) this.env.clearTimeout(this.renameTimer);
      this.renameTimer = null;
      if (this.renameObserver) this.Zotero.Notifier.unregisterObserver(this.renameObserver);
      this.renameObserver = null; this.changedAttachments.clear();
      if (this.timer) this.env.clearTimeout(this.timer);
      this.timer = null;
      for (const win of [...this.windows.keys()]) this.removeWindow(win);
      if (this.uiWindow && !this.uiWindow.closed) this.uiWindow.close();
      this.uiWindow = null;
      this.listeners.clear(); this.secrets.clear();
    }
  }
  global.PaperBridgeController = PaperBridgeController;
  if (typeof module !== 'undefined') module.exports = { PaperBridgeController, DEFAULTS, ACTIVE, TAG };
})(this);
