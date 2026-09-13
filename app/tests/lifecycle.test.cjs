const { test } = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const { EventEmitter } = require('node:events');
const source = fs.readFileSync(require('node:path').join(__dirname, '..', 'main.js'), 'utf8');
function harness() {
  const app = new EventEmitter();
  Object.assign(app, { requestSingleInstanceLock: () => true, whenReady: () => ({ then() {} }), getPath: () => '/tmp/phantom-test', quit() {} });
  const children = [], windows = [];
  class Window extends EventEmitter {
    constructor() {
      super(); windows.push(this); this.webContents = new EventEmitter();
      Object.assign(this.webContents, { setWindowOpenHandler() {}, session: { setPermissionRequestHandler() {} } });
    }
    isDestroyed() { return false; }
    async loadURL() {}
    show() { this.shown = true; }
    focus() {}
    hide() { this.hidden = true; }
  }
  const deps = {
    electron: { app, BrowserWindow: Window, dialog: { showMessageBox: async () => ({ response: 1 }) } },
    child_process: { spawn() { const child = new EventEmitter(); child.stdout = new EventEmitter(); child.stderr = new EventEmitter(); child.exitCode = null; child.signalCode = null; child.kill = () => { child.exitCode = 0; child.emit('exit', 0); }; children.push(child); return child; } },
    fs: { existsSync: () => true, mkdirSync() {}, createWriteStream: () => Object.assign(new EventEmitter(), { write() {}, end() {} }) },
    http: { get(_opts, callback) { const req = new EventEmitter(); queueMicrotask(() => callback({ statusCode: 200, resume() {} })); return req; } },
    net: { createServer() { const s = new EventEmitter(); s.listen = (_p, _h, callback) => callback(); s.address = () => ({port: 12345}); s.close = (cb) => cb(); return s; } },
  };
  const context = vm.createContext({ require: name => deps[name] || require(name), __dirname: '/tmp/app', process: { platform: 'darwin', env: {} }, console, setTimeout, clearTimeout, Buffer });
  vm.runInContext(source + '\nthis.testAPI = {createWindow,stopBackend,waitForHealth};', context);
  return { ...context.testAPI, app, windows, children };
}
test('close and activate preserves one engine and window', async () => {
  const h = harness(); await h.createWindow();
  let prevented = false; h.windows[0].emit('close', {preventDefault() { prevented = true; }});
  await h.createWindow();
  assert.equal(prevented, true); assert.equal(h.windows[0].hidden, true);
  assert.equal(h.windows.length, 1); assert.equal(h.children.length, 1);
  await h.stopBackend(); assert.equal(h.children[0].exitCode, 0);
});
test('concurrent activation does not spawn duplicate engines', async () => {
  const h = harness(); await Promise.all([h.createWindow(), h.createWindow()]);
  assert.equal(h.children.length, 1); await h.stopBackend();
});
