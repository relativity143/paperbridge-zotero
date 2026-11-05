/* SPDX-License-Identifier: AGPL-3.0-or-later */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const crypto = require('node:crypto');
const source = fs.readFileSync(path.join(__dirname, '../addon/content/paperbridge.js'), 'utf8');

function fixture() {
  const prefs = new Map(), passwords = [], items = new Map(), files = new Set(['/source.pdf']);
  const imports = [], requests = [], readers = [], backups = [], renames = [], timers = new Map();
  let seq = 100, timerSeq = 1;
  const state = { bridge: { url: 'http://127.0.0.1:19876', token: 'companion-token-not-for-prefs' },
    failMono: false, httpError: null, response: null, saveFailure: false, backupFailure: false };
  const attachment = (id, extra = {}) => ({ id, libraryID: 7, parentItemID: 5,
    attachmentContentType: 'application/pdf', attachmentFilename: 'source.pdf', deleted: false,
    tags: new Set(), relations: {}, isAttachment: () => true, isRegularItem: () => false,
    fields: { title: 'Research <script>paper</script>' },
    getFilePathAsync: async () => '/source.pdf', getField(key) { return this.fields[key] || ''; },
    setField(key, value) { this.fields[key] = value; },
    async renameAttachmentFile(name, options) {
      renames.push({ id: this.id, name, options });
      this.attachmentFilename = state.collision ? name.replace(/\.pdf$/, ' 2.pdf') : name;
      this.storagePath = 'storage:' + this.attachmentFilename;
      return true;
    },
    getCollections: () => [11, 12], hasTag(tag) { return this.tags.has(tag); },
    getRelations() { return this.relations; }, addTag(tag) { this.tags.add(tag); },
    addRelation(predicate, object) { this.relations[predicate] = [object]; },
    removeRelation(predicate, object) { this.relations[predicate] = (this.relations[predicate] || []).filter(v => v !== object); },
    async saveTx() { if (state.saveFailure) throw new Error('cannot save metadata'); }, ...extra });
  items.set(5, { id: 5, libraryID: 7, fields: { title: 'Very Cold Trapped Atoms in a Vapor Cell', date: '1990-09-24', language: 'en', publicationTitle: 'Physical Review Letters' },
    getField(key) { return this.fields[key] || ''; }, getCreators: () => [{ lastName: 'Monroe', creatorType: 'author' }, { lastName: 'Wieman', creatorType: 'author' }],
    isAttachment: () => false, isRegularItem: () => true, getAttachments: () => [6] });
  items.set(6, attachment(6));
  const env = {
    URL, crypto, PathUtils: { join: path.join },
    setTimeout: (fn, delay) => { const id = timerSeq++; timers.set(id, { fn, delay }); return id; },
    clearTimeout: id => timers.delete(id),
    Components: { interfaces: { nsIFile: {}, nsILoginInfo: {} }, classes: {
      '@mozilla.org/login-manager/loginInfo;1': { createInstance: () => ({ init(origin, action, realm, username, password) {
        Object.assign(this, { origin, action, realm, username, password });
      } }) }
    } },
    Services: { dirsvc: { get: () => ({ path: '/Users/test' }) }, logins: {
      findLogins: (origin, action, realm) => passwords.filter(p => p.realm === realm),
      addLoginAsync: async p => passwords.push(p),
      modifyLogin: (old, p) => passwords.splice(passwords.indexOf(old), 1, p),
      removeLogin: old => passwords.splice(passwords.indexOf(old), 1)
    } },
    IOUtils: { readJSON: async () => state.bridge, exists: async p => files.has(p),
      makeDirectory: async () => {}, setPermissions: async () => {},
      writeJSON: async (path, data) => { if (state.backupFailure) throw new Error('backup failed'); backups.push({ path, data: JSON.parse(JSON.stringify(data)) }); },
      read: async () => Uint8Array.from(Buffer.from('%PDF-')) },
    Zotero: {
      Prefs: { get: key => prefs.get(key), set: (key, value) => prefs.set(key, value) },
      HTTP: { request: async (method, url, options) => {
        requests.push({ method, url, options });
        if (state.httpError) throw state.httpError;
        if (state.response) return state.response;
        const body = options.body && JSON.parse(options.body);
        return { status: 200, response: body?.id ? { id: body.id, status: 'queued', progress: null, stage: 'Queued' } : { ok: true, engine: { available: true, version: '0.6.4' } } };
      } },
      Items: { getAsync: async id => items.get(id) },
      Libraries: { get: () => ({ editable: true, filesEditable: true }) },
      Relations: { getByPredicateAndObject: async (type, predicate, object) => [...items.values()].filter(i => i.relations?.[predicate]?.includes(object)) },
      DB: { columnQueryAsync: async (sql, [storagePath]) => [...items.values()].filter(i => sql.includes('itemRelations')
        ? (i.relations?.['dc:relation'] || []).some(v => v.startsWith('urn:paperbridge:'))
        : sql.includes('itemTags') ? i.tags?.has(storagePath) : i.storagePath === storagePath).map(i => i.id) },
      Attachments: { importFromFile: async options => {
        if (state.failMono && options.fileBaseName.endsWith('-mono')) throw new Error('disk full');
        imports.push(options);
        const item = attachment(seq++, { libraryID: options.libraryID, parentItemID: options.parentItemID || null,
          fields: { title: options.title }, attachmentFilename: options.fileBaseName + '.pdf', storagePath: 'storage:' + options.fileBaseName + '.pdf' });
        items.set(item.id, item); return item;
      } },
      Reader: { open: async id => readers.push(id) },
      getMainWindow: () => ({ ZoteroPane: { getSelectedItems: () => [items.get(5)] } })
    }
  };
  const sandbox = vm.createContext({});
  vm.runInContext(source, sandbox);
  const c = new sandbox.PaperBridgeController(env); c.init();
  return { c, env, prefs, passwords, items, files, imports, requests, readers, backups, renames, timers, state, attachment };
}
async function completed(f, standalone = false) {
  if (standalone) f.items.get(6).parentItemID = null;
  await f.c.queueItems([f.items.get(6)]);
  const t = f.c.tasks[0]; t.status = 'completed';
  t.outputs = { dual: '/dual.pdf', mono: '/mono.pdf' }; f.files.add('/dual.pdf'); f.files.add('/mono.pdf');
  return t;
}

test('configuration rejects invalid pages, non-loopback cleartext API, and embedded credentials', () => {
  const { c } = fixture(), config = c.getSettings();
  for (const patch of [{ pages: '0' }, { pages: '3-1' }, { pages: '1;rm' }, { qps: 0 }, { baseUrl: 'http://example.org/v1' }, { baseUrl: 'https://x:secret@example.org' }, { baseUrl: 'https://example.org?key=x' }, { dualMode: 'unknown' }]) {
    assert.throws(() => c.validateSettings({ ...config, ...patch }));
  }
  assert.equal(c.validateSettings({ ...config, baseUrl: 'http://localhost:11434/v1/', pages: '1-3, 5' }).pages, '1-3,5');
});

test('API key is kept in login manager, scoped to API URL, and excluded from persisted settings/tasks', async () => {
  const f = fixture(), { c } = f;
  await c.saveSettings(c.getSettings(), 'secret-api-key', 'replace');
  assert.equal(c.getApiKey(c.getSettings().baseUrl), 'secret-api-key');
  assert.equal(c.getApiKey('https://other.example/v1'), '');
  await c.queueItems([f.items.get(5)]); await c.startReady();
  const persisted = [...f.prefs.values()].join(' ');
  assert.ok(!persisted.includes('secret-api-key'));
  assert.ok(!persisted.includes(f.state.bridge.token));
  assert.equal(f.passwords.length, 1);
  await c.saveSettings(c.getSettings(), '', 'clear'); assert.equal(f.passwords.length, 0);
});

test('queuing selects child PDFs and skips own output, duplicate, missing, and non-PDF attachments without sending', async () => {
  const f = fixture();
  f.items.set(7, f.attachment(7, { attachmentFilename: 'paperbridge-abc-dual.pdf' }));
  f.items.set(8, f.attachment(8, { attachmentContentType: 'text/html' }));
  assert.equal(await f.c.queueItems([f.items.get(5), f.items.get(6), f.items.get(7), f.items.get(8)]), 1);
  assert.equal(f.c.tasks[0].status, 'ready'); assert.equal(f.requests.length, 0);
  assert.equal(await f.c.queueItems([f.items.get(5)]), 0);
});

test('mono output defaults off, persists opt-in and travels with submitted task settings', async () => {
  const f = fixture(), { c } = f;
  assert.equal(c.getSettings().generateMono, false);
  assert.throws(() => c.validateSettings({ ...c.getSettings(), generateMono: 'false' }));
  await c.saveSettings({ ...c.getSettings(), generateMono: true });
  assert.equal(c.getSettings().generateMono, true);
  await c.queueItems([f.items.get(6)]); await c.startReady();
  assert.equal(JSON.parse(f.requests[0].options.body).config.generateMono, true);
  await c.saveSettings({ ...c.getSettings(), generateMono: false });
  assert.equal(c.getSettings().generateMono, false);
  assert.equal(c.tasks[0].config.generateMono, true, 'Existing submitted job keeps its chosen outputs');
});

test('bilingual-only result imports one attachment and retry creates no duplicate', async () => {
  const f = fixture(), t = await completed(f);
  f.c.uiWindow = { closed: false, document: { hidden: false, hasFocus: () => true } };
  delete t.outputs.mono;
  t.config = f.c.getSettings();
  await f.c.importTask(t.id);
  assert.equal(t.importState, 'done'); assert.equal(f.imports.length, 1);
  assert.ok(t.imported.dual); assert.equal(t.imported.mono, undefined);
  await f.c.importTask(t.id);
  assert.equal(f.imports.length, 1); assert.equal(f.readers.length, 1);
});

test('explicitly requested mono result cannot silently disappear during import', async () => {
  const f = fixture(), t = await completed(f);
  delete t.outputs.mono;
  t.config = { ...f.c.getSettings(), generateMono: true };
  await f.c.importTask(t.id);
  assert.equal(t.importState, 'error'); assert.equal(f.imports.length, 1);
});

test('authenticated request suppresses request body logs and redirects; local APIs can omit key', async () => {
  const f = fixture(); await f.c.queueItems([f.items.get(6)]); await f.c.startReady();
  const { options, url } = f.requests[0];
  assert.equal(options.logBodyLength, 0); assert.equal(options.debug, false); assert.equal(options.followRedirects, false);
  assert.equal(options.anon, true); assert.equal(options.headers.Authorization, 'Bearer ' + f.state.bridge.token);
  assert.equal(JSON.parse(options.body).config.apiKey, ''); assert.ok(url.startsWith('http://127.0.0.1:'));
});

test('companion pairing rejects remote hosts, credentials, paths and weak tokens before any request', async () => {
  for (const patch of [{ url: 'https://example.org' }, { url: 'http://127.0.0.1/other' }, { url: 'http://x:y@127.0.0.1' }, { token: 'short' }]) {
    const f = fixture(); Object.assign(f.state.bridge, patch);
    await assert.rejects(() => f.c.checkConnection()); assert.equal(f.requests.length, 0);
  }
});

test('uncertain submission reuses task ID, preserves original config and does not create another local task', async () => {
  const f = fixture(); await f.c.queueItems([f.items.get(6)]);
  f.state.httpError = new Error('network timeout'); await f.c.startReady();
  const id = f.c.tasks[0].id; assert.equal(f.c.tasks[0].status, 'unknown');
  await f.c.saveSettings({ ...f.c.getSettings(), model: 'other-model' });
  f.state.httpError = null; await f.c.startTask(id);
  assert.equal(f.c.tasks.length, 1);
  const bodies = f.requests.map(r => JSON.parse(r.options.body));
  assert.equal(bodies[0].id, bodies[1].id); assert.equal(bodies[0].config.model, bodies[1].config.model);
});

test('server missing task becomes manually retryable without endless polling', async () => {
  const f = fixture(); await f.c.queueItems([f.items.get(6)]); await f.c.startReady();
  f.state.response = { status: 404, response: { error: 'missing' } };
  await f.c.refreshJobs(); assert.equal(f.c.tasks[0].status, 'submit_failed');
  const count = f.requests.length; await f.c.refreshJobs(); assert.equal(f.requests.length, count);
});

test('partial import retries only missing file, preserves original and opens bilingual result once', async () => {
  const f = fixture(), t = await completed(f); f.state.failMono = true;
  f.c.uiWindow = { closed: false, document: { hidden: false, hasFocus: () => true } };
  await f.c.importTask(t.id); assert.equal(t.importState, 'error'); assert.equal(f.imports.length, 1);
  assert.ok(f.items.has(6)); assert.equal(f.imports[0].parentItemID, 5); assert.equal(f.imports[0].collections, undefined);
  f.state.failMono = false; await f.c.importTask(t.id);
  assert.equal(t.importState, 'done'); assert.equal(f.imports.length, 2); assert.equal(f.readers.length, 1);
  await f.c.importTask(t.id); assert.equal(f.imports.length, 2); assert.equal(f.readers.length, 1);
  for (const id of Object.values(t.imported)) {
    assert.deepEqual(f.items.get(id).getRelations(), {}, 'Generated attachments must not add non-Zotero relations that break cloud sync');
  }
});

test('migration backs up and removes only legacy PaperBridge markers, preserving real and unrelated relations', async () => {
  const f = fixture(), item = f.items.get(6);
  const marker = 'urn:paperbridge:' + crypto.randomUUID() + ':dual';
  const real = 'http://zotero.org/users/1/items/ABCDEFGH';
  item.relations = { 'dc:relation': [marker, real, 'urn:other:keep', 'urn:paperbridge:unknown'], 'owl:sameAs': [real] };
  const result = await f.c.repairLegacyRelations();
  assert.equal(result.items, 1); assert.equal(result.relations, 1);
  assert.deepEqual(item.relations, { 'dc:relation': [real, 'urn:other:keep', 'urn:paperbridge:unknown'], 'owl:sameAs': [real] });
  assert.deepEqual(f.backups[0].data.entries[0].values, [marker]);
  assert.equal(f.backups[0].data.entries[0].itemID, 6);
  await f.c.repairLegacyRelations(); assert.equal(f.backups.length, 1);
  assert.equal(f.requests.length, 0); assert.equal(f.imports.length, 0);
});

test('migration makes no library changes when its backup cannot be saved', async () => {
  const f = fixture(), item = f.items.get(6);
  const marker = 'urn:paperbridge:' + crypto.randomUUID() + ':mono';
  item.relations = { 'dc:relation': [marker] }; f.state.backupFailure = true;
  await assert.rejects(() => f.c.repairLegacyRelations(), /backup failed/);
  assert.deepEqual(item.relations['dc:relation'], [marker]);
  assert.equal(f.c.prefGet('relationRepairVersion', 0), 0);
});

test('standalone PDF imports keep its library and collections', async () => {
  const f = fixture(), t = await completed(f, true); await f.c.importTask(t.id);
  assert.equal(f.imports.length, 2);
  for (const options of f.imports) {
    assert.equal(options.libraryID, 7); assert.equal(options.parentItemID, undefined);
    assert.equal(JSON.stringify(options.collections), '[11,12]');
  }
});

test('crash between attachment commit and metadata save is recovered by deterministic filename', async () => {
  const f = fixture(), t = await completed(f); f.state.saveFailure = true;
  await f.c.importTask(t.id); assert.equal(f.imports.length, 1);
  const first = [...f.items.values()].find(i => i.storagePath); first.relations = {}; first.tags.clear();
  t.imported = {}; f.state.saveFailure = false;
  await f.c.importTask(t.id); assert.equal(t.importState, 'done'); assert.equal(f.imports.length, 2);
  assert.equal(t.imported.dual, first.id);
});

test('polling continues when window closes; failures back off and shutdown clears timers', async () => {
  const f = fixture(); await f.c.queueItems([f.items.get(6)]); await f.c.startReady();
  assert.equal(f.timers.size, 1);
  f.c.uiWindow = { closed: false, document: { hidden: false }, close() { this.closed = true; } };
  f.c.schedule(); assert.equal([...f.timers.values()][0].delay, 2000);
  const win = f.c.uiWindow; f.c.windowClosed(win);
  assert.equal(f.timers.size, 1); assert.equal(f.c.uiWindow, null);
  f.state.httpError = new Error('offline'); await f.c.refreshJobs(); assert.equal([...f.timers.values()][0].delay, 4000);
  f.c.shutdown(); assert.equal(f.timers.size, 0); assert.equal(f.c.uiWindow, null); assert.equal(f.c.listeners.size, 0);
});

test('menu lifecycle is idempotent and removes nodes/listeners on disable', () => {
  const f = fixture(), nodes = [], parents = new Map(['menu_ToolsPopup', 'zotero-itemmenu'].map(k => [k, { appendChild: n => nodes.push(n) }]));
  let removed = 0, detached = 0;
  const win = { document: { getElementById: id => parents.get(id), createXULElement: () => ({
    setAttribute() {}, addEventListener() {}, removeEventListener() { detached++; }, remove() { removed++; }
  }) } };
  f.c.addWindow(win); f.c.addWindow(win); assert.equal(nodes.length, 2);
  f.c.shutdown(); assert.equal(removed, 2); assert.equal(detached, 2); assert.equal(f.c.windows.size, 0);
});

test('restoring task metadata resumes uncertain submit and interrupted import without API secrets', async () => {
  const f = fixture(); await f.c.queueItems([f.items.get(6)]);
  f.c.tasks[0].status = 'submitting'; f.c.tasks[0].importState = 'importing'; f.c.persist();
  f.c.tasks = []; f.c.init(); assert.equal(f.c.tasks[0].status, 'unknown'); assert.equal(f.c.tasks[0].importState, 'pending');
});

test('terminal failures never auto-retry or transmit PDF again', async () => {
  const f = fixture(); await f.c.queueItems([f.items.get(6)]); await f.c.startReady();
  const t = f.c.tasks[0]; f.state.response = { status: 200, response: { id: t.id, status: 'failed', error: 'provider rejected', progress: 0, stage: 'failed' } };
  await f.c.refreshJobs(); const count = f.requests.length;
  await f.c.refreshJobs(); await f.c.startReady(); assert.equal(f.requests.length, count); assert.equal(t.status, 'failed');
});

test('manifest meets Zotero 9 required metadata, including its mandatory update_url', () => {
  // Zotero-patched Extension.sys.mjs rejects addons missing any of these fields.
  const manifest = JSON.parse(fs.readFileSync(path.join(__dirname, '../addon/manifest.json'), 'utf8'));
  const zotero = manifest.applications.zotero;
  assert.equal(zotero.id, 'paperbridge@local');
  assert.ok(zotero.strict_max_version);
  assert.ok(zotero.update_url);
  assert.equal(new URL(zotero.update_url).hostname, 'paperbridge.invalid');
});

test('English journal results use author, year and English title without a model request', async () => {
  const f = fixture();
  f.items.get(5).fields.title = '蒸气池中的极冷囚禁原子';
  f.items.get(5).fields.extra = 'Original Title: Very Cold Trapped Atoms in a Vapor Cell';
  const t = await completed(f); delete t.outputs.mono;
  await f.c.importTask(t.id);
  const item = f.items.get(t.imported.dual);
  assert.equal(t.importState, 'done');
  assert.equal(item.getField('title'), 'Monroe et al. - 1990 - Very Cold Trapped Atoms in a Vapor Cell');
  assert.equal(item.attachmentFilename, item.getField('title') + '.pdf');
  assert.equal(f.requests.length, 0);
  assert.equal(f.items.get(6).attachmentFilename, 'source.pdf');
});

test('Chinese journals retain Chinese title and complete Chinese author name', async () => {
  const f = fixture(), parent = f.items.get(5);
  parent.fields = { title: 'Cold atom trapping', extra: '中文标题: 冷原子的俘获', publicationTitle: '物理学报', language: 'zh-CN', date: '2024-03' };
  parent.getCreators = () => [{lastName:'张',firstName:'三',creatorType:'author'},{lastName:'李',firstName:'四',creatorType:'author'}];
  const t = await completed(f); await f.c.importTask(t.id);
  assert.equal(f.items.get(t.imported.dual).getField('title'), '张三 等 - 2024 - 冷原子的俘获');
  assert.equal(f.items.get(t.imported.mono).attachmentFilename, '张三 等 - 2024 - 冷原子的俘获 (translation).pdf');
});

test('standalone files without metadata use explicit missing fields and safe bounded filenames', async () => {
  const f = fixture(); f.items.get(6).fields.title = '<b>扫描件</b> / A:B? ' + '标题'.repeat(100);
  const t = await completed(f, true); await f.c.importTask(t.id);
  const item = f.items.get(t.imported.dual);
  assert.ok(item.attachmentFilename.startsWith('Unknown author - n.d. - 扫描件 A B'));
  assert.ok(!/[<>:*?/\\]/.test(item.attachmentFilename));
  assert.ok(Buffer.byteLength(item.attachmentFilename, 'utf8') <= 255);
});

test('existing files are never overwritten and a unique name is stable on import retry', async () => {
  const f = fixture(); f.state.collision = true;
  const t = await completed(f); delete t.outputs.mono; await f.c.importTask(t.id);
  const name = f.items.get(t.imported.dual).attachmentFilename;
  assert.ok(name.endsWith(' 2.pdf'));
  assert.equal(f.renames[0].options.overwrite, false); assert.equal(f.renames[0].options.unique, true);
  await f.c.importTask(t.id);
  assert.equal(f.renames.length, 1); assert.equal(f.imports.length, 1);
  assert.equal(f.items.get(t.imported.dual).attachmentFilename, name);
});

test('file organizer replacement is recovered by marker and renamed without duplicate import', async () => {
  const f = fixture(), t = await completed(f); delete t.outputs.mono; await f.c.importTask(t.id);
  const old = f.items.get(t.imported.dual); f.items.delete(old.id);
  const clone = f.attachment(200, {tags: new Set(old.tags), fields:{title:'organizer.pdf'},attachmentFilename:'organizer.pdf'});
  f.items.set(200, clone);
  assert.equal((await f.c.findImported(t,'dual')).id, 200);
  await f.c.reconcileNamedAttachments([200]);
  assert.equal(t.imported.dual, 200);
  assert.equal(clone.attachmentFilename, t.naming.base + '.pdf');
  await f.c.importTask(t.id);
  assert.equal(f.imports.length, 1);
});

test('attachment naming observer is event-driven and removed on shutdown', () => {
  const f = fixture(); let observer, registrations = 0, removals = 0;
  f.env.Zotero.Notifier = {
    registerObserver(o) { observer = o; registrations++; return 'naming-observer'; },
    unregisterObserver(id) { assert.equal(id, 'naming-observer'); removals++; }
  };
  f.c.watchNamedAttachments(); f.c.watchNamedAttachments();
  assert.equal(registrations, 1); assert.equal(f.timers.size, 0);
  observer.notify('add', 'item', [200]); assert.equal(f.timers.size, 1);
  f.c.shutdown(); assert.equal(removals, 1); assert.equal(f.timers.size, 0);
  assert.equal(f.c.changedAttachments.size, 0);
});

test('closed translation window still polls, imports completed PDF and leaves the reader alone', async () => {
  const f = fixture(); await f.c.queueItems([f.items.get(6)]); await f.c.startReady();
  const t = f.c.tasks[0];
  f.c.windowClosed(null);
  assert.equal(f.timers.size, 1, 'A task must keep polling without a translation window');
  f.files.add('/dual.pdf');
  f.state.response = { status: 200, response: { id: t.id, status: 'completed', outputs: { dual: '/dual.pdf' } } };
  const [id, timer] = [...f.timers][0]; f.timers.delete(id); await timer.fn();
  await new Promise(setImmediate);
  assert.equal(t.importState, 'done'); assert.equal(f.imports.length, 1);
  assert.equal(f.readers.length, 0, 'Background import must not open or focus a reader tab');
  assert.equal(f.timers.size, 0, 'An idle queue needs no polling');
});

test('restart resumes pending import without opening the translation window', async () => {
  const f = fixture(), t = await completed(f); delete t.outputs.mono;
  f.c.persist(); f.c.tasks = []; f.c.init();
  assert.equal(f.c.uiWindow, null);
  assert.equal(f.timers.size, 1, 'Saved pending work resumes on controller initialization');
  const [id, timer] = [...f.timers][0]; f.timers.delete(id); await timer.fn();
  await new Promise(setImmediate);
  assert.equal(f.c.tasks[0].importState, 'done'); assert.equal(f.imports.length, 1);
  assert.equal(f.requests.length, 0, 'Completed output import does not resubmit paid work');
  assert.equal(f.readers.length, 0);
});

test('batch submission allows two requests in flight, and submits each selected PDF once', async () => {
  const f = fixture();
  for (const id of [7, 8]) f.items.set(id, f.attachment(id));
  await f.c.queueItems([6, 7, 8].map(id => f.items.get(id)));
  const releases = [], submitted = []; let inFlight = 0, peak = 0;
  f.env.Zotero.HTTP.request = async (method, url, options) => {
    const body = JSON.parse(options.body); submitted.push(body.id);
    inFlight++; peak = Math.max(peak, inFlight);
    await new Promise(resolve => releases.push(resolve)); inFlight--;
    return { status: 202, response: { id: body.id, status: 'queued' } };
  };
  const batch = f.c.startReady(); await new Promise(setImmediate);
  assert.equal(submitted.length, 2, 'A slow first submission must not block the next PDF');
  releases.splice(0).forEach(resolve => resolve()); await new Promise(setImmediate);
  assert.equal(submitted.length, 3);
  releases.splice(0).forEach(resolve => resolve()); await batch;
  assert.equal(peak, 2); assert.equal(new Set(submitted).size, 3);
  assert.ok(f.c.tasks.every(t => t.status === 'queued')); f.c.shutdown();
});

test('status checks are bounded to four concurrent requests and service delay does not serialize the batch', async () => {
  const f = fixture();
  for (const id of [7, 8, 9, 10]) f.items.set(id, f.attachment(id));
  await f.c.queueItems([6, 7, 8, 9, 10].map(id => f.items.get(id))); await f.c.startReady();
  const releases = []; let calls = 0, inFlight = 0, peak = 0;
  f.env.Zotero.HTTP.request = async (method, url) => {
    assert.equal(method, 'GET'); calls++; inFlight++; peak = Math.max(peak, inFlight);
    await new Promise(resolve => releases.push(resolve)); inFlight--;
    return { status: 200, response: { id: url.split('/').pop(), status: 'running' } };
  };
  const refresh = f.c.refreshJobs(); await new Promise(setImmediate);
  assert.equal(calls, 4); releases.splice(0).forEach(resolve => resolve());
  await new Promise(setImmediate); assert.equal(calls, 5);
  releases.splice(0).forEach(resolve => resolve()); await refresh;
  assert.equal(peak, 4); f.c.shutdown();
});

test('a poll returning after shutdown cannot import, persist changes or restart polling', async () => {
  const f = fixture(); await f.c.queueItems([f.items.get(6)]); await f.c.startReady();
  const t = f.c.tasks[0], saved = f.prefs.get('extensions.paperbridge.tasks');
  let release;
  f.env.Zotero.HTTP.request = async () => {
    await new Promise(resolve => { release = resolve; });
    return { status: 200, response: { id: t.id, status: 'completed', outputs: { dual: '/dual.pdf' } } };
  };
  f.files.add('/dual.pdf');
  const refresh = f.c.refreshJobs(); await new Promise(setImmediate);
  f.c.shutdown(); release(); await refresh;
  assert.equal(f.imports.length, 0); assert.equal(f.timers.size, 0);
  assert.equal(f.prefs.get('extensions.paperbridge.tasks'), saved);
});

test('visible but unfocused translation window imports without taking over the reader', async () => {
  const f = fixture(), t = await completed(f); delete t.outputs.mono;
  f.c.uiWindow = { closed: false, document: { hidden: false, hasFocus: () => false } };
  await f.c.importTask(t.id);
  assert.equal(t.importState, 'done'); assert.equal(f.readers.length, 0);
});
