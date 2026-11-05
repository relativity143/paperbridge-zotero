/* SPDX-License-Identifier: AGPL-3.0-or-later */
var chromeHandle;
var controller;
function install() {}
function uninstall() {}
async function startup({ rootURI }) {
  await Zotero.initializationPromise;
  const startupService = Components.classes['@mozilla.org/addons/addon-manager-startup;1']
    .getService(Components.interfaces.amIAddonManagerStartup);
  chromeHandle = startupService.registerChrome(Services.io.newURI(rootURI + 'manifest.json'), [
    ['content', 'paperbridge', rootURI + 'content/']
  ]);
  const scope = {};
  // The XPI URL stays unchanged on update. Do not mix a fresh bootstrap with
  // the previous version's cached controller (Zotero also bypasses this cache).
  Services.scriptloader.loadSubScriptWithOptions(rootURI + 'content/paperbridge.js', {
    target: scope, ignoreCache: true
  });
  controller = new scope.PaperBridgeController({ Zotero, Services, Components, IOUtils, PathUtils,
    crypto, URL, setTimeout, clearTimeout });
  Zotero.PaperBridge = controller;
  controller.init();
  controller.watchNamedAttachments();
  try { await controller.repairLegacyRelations(); }
  catch (error) { controller.report('旧版关联标记修复未完成：' + controller.safeMessage(error)); }
  for (const window of Zotero.getMainWindows()) controller.addWindow(window);
}
function onMainWindowLoad({ window }) { controller?.addWindow(window); }
function onMainWindowUnload({ window }) { controller?.removeWindow(window); }
function shutdown() {
  controller?.shutdown();
  delete Zotero.PaperBridge;
  controller = null;
  chromeHandle?.destruct();
  chromeHandle = null;
}
