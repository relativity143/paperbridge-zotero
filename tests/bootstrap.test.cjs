/* SPDX-License-Identifier: AGPL-3.0-or-later */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const bootstrap = fs.readFileSync(path.join(__dirname, '../addon/bootstrap.js'), 'utf8');
const controller = fs.readFileSync(path.join(__dirname, '../addon/content/paperbridge.js'), 'utf8');

test('bootstrap bypasses a previous controller cached at the same XPI URL and restores both menus', async () => {
  const nodes = new Map();
  const parent = { appendChild(node) { nodes.set(node.id, node); } };
  const win = { document: {
    getElementById(id) { return ['menu_ToolsPopup', 'zotero-itemmenu'].includes(id) ? parent : nodes.get(id); },
    createXULElement() { return { setAttribute() {}, addEventListener() {}, removeEventListener() {}, remove() {} }; }
  } };
  const loads = [];
  const scope = vm.createContext({
    Zotero: { initializationPromise: Promise.resolve(), Prefs: { get: key => key.endsWith('relationRepairVersion') ? 1 : undefined },
      getMainWindows: () => [win], Notifier: { registerObserver: () => 'observer', unregisterObserver() {} } },
    Components: { interfaces: {}, classes: { '@mozilla.org/addons/addon-manager-startup;1': {
      getService: () => ({ registerChrome: () => ({ destruct() {} }) })
    } } },
    Services: { io: { newURI: x => x }, scriptloader: {
      loadSubScript(uri, target) {
        loads.push({ uri, ignoreCache: false });
        // Before 0.1.3 this class did not have the new naming observer.
        target.PaperBridgeController = class { init() {} };
      },
      loadSubScriptWithOptions(uri, options) {
        loads.push({ uri, ignoreCache: options.ignoreCache });
        if (!options.ignoreCache) return this.loadSubScript(uri, options.target);
        vm.runInNewContext(controller, options.target);
      }
    } },
    IOUtils: {}, PathUtils: {}, crypto: {}, URL, setTimeout, clearTimeout
  });
  vm.runInContext(bootstrap, scope);
  await scope.startup({ rootURI: 'jar:file:///test/paperbridge@local.xpi!/' });
  assert.equal(loads.length, 1);
  assert.equal(loads[0].ignoreCache, true);
  assert.equal(typeof scope.Zotero.PaperBridge.watchNamedAttachments, 'function');
  assert.ok(nodes.has('paperbridge-tools'));
  assert.ok(nodes.has('paperbridge-translate'));
  scope.shutdown();
});
