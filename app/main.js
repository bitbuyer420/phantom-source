"use strict";
const { app, BrowserWindow, dialog } = require("electron");
const { spawn } = require("child_process");
const fs = require("fs");
const path = require("path");
const http = require("http");
const net = require("net");
const crypto = require("crypto");

const ENGINE_DIR = path.join(__dirname, "..", "engine");
const PY = path.join(ENGINE_DIR, ".venv", "bin", "python");
let backend = null;
let win = null;
let creating = false;
let quitting = false;
let logStream = null;

function runtimeCommand(port) {
  const executable = app.isPackaged
    ? path.join(process.resourcesPath, "bin", "phantom-runtime", "phantom-runtime")
    : PY;
  if (!fs.existsSync(executable)) throw new Error("Phantom's engine is missing. Reinstall Phantom.");
  return {
    executable,
    args: app.isPackaged ? ["--port", String(port)] : ["-m", "phantom.server", "--port", String(port)],
    cwd: app.isPackaged ? path.dirname(executable) : ENGINE_DIR,
    env: app.isPackaged ? { PHANTOM_RUNTIME_EXECUTABLE: executable } : {},
  };
}

function freePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const { port } = server.address();
      server.close(() => resolve(port));
    });
  });
}

function waitForHealth(port, token, child, tries = 80) {
  return new Promise((resolve, reject) => {
    let finished = false;
    let timer;
    const finish = (error) => {
      if (finished) return;
      finished = true;
      clearTimeout(timer);
      child.removeListener("error", onError);
      child.removeListener("exit", onExit);
      error ? reject(error) : resolve();
    };
    const onError = () => finish(new Error("The engine could not be launched."));
    const onExit = () => finish(new Error("The engine stopped during startup."));
    child.once("error", onError);
    child.once("exit", onExit);
    const attempt = (remaining) => {
      if (finished) return;
      let handled = false;
      const retry = () => {
        if (handled || finished) return;
        handled = true;
        if (remaining <= 0) finish(new Error("The engine did not become ready."));
        else timer = setTimeout(() => attempt(remaining - 1), 400);
      };
      const request = http.get({ host: "127.0.0.1", port, path: "/api/health", timeout: 800,
        headers: { "X-Phantom-Token": token } }, (response) => {
        response.resume();
        if (response.statusCode === 200) { handled = true; finish(); }
        else retry();
      });
      request.once("error", retry);
      request.once("timeout", () => { request.destroy(); retry(); });
    };
    attempt(tries);
  });
}

async function stopBackend() {
  const child = backend;
  backend = null;
  if (!child || child.exitCode !== null || child.signalCode !== null) return;
  await new Promise((resolve) => {
    let timer;
    const done = () => { clearTimeout(timer); resolve(); };
    child.once("exit", done);
    child.once("error", done);
    timer = setTimeout(() => { child.kill("SIGKILL"); resolve(); }, 2500);
    child.kill("SIGTERM");
  });
}

async function createWindow() {
  if (creating || quitting) return;
  if (win && !win.isDestroyed()) { win.show(); win.focus(); return; }
  creating = true;
  try {
    await stopBackend();
    const port = await freePort();
    const token = crypto.randomBytes(24).toString("hex");
    const runtime = runtimeCommand(port);
    const logs = app.getPath("logs");
    fs.mkdirSync(logs, { recursive: true });
    if (logStream) logStream.end();
    // One bounded log per launch; no route fixes or authentication tokens.
    logStream = fs.createWriteStream(path.join(logs, "engine.log"), { flags: "w", mode: 0o600 });
    logStream.on("error", (error) => console.error("Phantom log unavailable:", error.code));
    let logBytes = 0;
    const log = (chunk) => {
      if (logBytes >= 1024 * 1024) return;
      const text = String(chunk).replaceAll(token, "[redacted]");
      logBytes += Buffer.byteLength(text);
      logStream.write(text);
    };
    const child = spawn(runtime.executable, runtime.args, {
      cwd: runtime.cwd, stdio: ["ignore", "pipe", "pipe"],
      env: { ...process.env, ...runtime.env, PHANTOM_TOKEN: token },
    });
    backend = child;
    child.stdout.on("data", log);
    child.stderr.on("data", log);
    child.on("error", (error) => log(`Engine launch failed: ${error.code}\n`));
    let engineReady = false;
    child.on("exit", async (code) => {
      if (quitting || backend !== child || !engineReady) return;
      log(`Engine exited: ${code}\n`);
      const result = await dialog.showMessageBox({ type: "error", title: "Phantom engine stopped",
        message: "The engine stopped unexpectedly",
        detail: "The device location is no longer confirmed. Restart the engine to reconnect, then review the session before continuing.",
        buttons: ["Restart engine", "Quit"], defaultId: 0, cancelId: 1 });
      if (quitting) return;
      if (result.response === 0) {
        if (win && !win.isDestroyed()) win.destroy();
        win = null;
        await createWindow();
      } else app.quit();
    });
    win = new BrowserWindow({
      width: 1240, height: 840, minWidth: 940, minHeight: 660,
      backgroundColor: "#111820", title: "Phantom",
      webPreferences: {
        preload: path.join(__dirname, "preload.js"), contextIsolation: true,
        nodeIntegration: false, sandbox: true,
        additionalArguments: ["--phantom-token=" + token],
      },
    });
    const current = win;
    // Closing the window preserves its one engine and session on macOS.
    current.on("close", (event) => {
      if (process.platform === "darwin" && !quitting) { event.preventDefault(); current.hide(); }
    });
    current.on("closed", () => { if (win === current) win = null; });
    const origin = `http://127.0.0.1:${port}`;
    current.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
    current.webContents.on("will-navigate", (event, url) => {
      if (url !== origin && !url.startsWith(origin + "/")) event.preventDefault();
    });
    current.webContents.session.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
    await waitForHealth(port, token, child);
    if (!current.isDestroyed()) await current.loadURL(origin);
    engineReady = true;
  } catch (error) {
    await stopBackend();
    if (win && !win.isDestroyed()) win.destroy();
    win = null;
    const result = await dialog.showMessageBox({ type: "error", title: "Phantom couldn't start",
      message: "Phantom couldn't start its engine",
      detail: `${error.message}\nRetry to restart the engine. Diagnostic details are in Phantom's engine.log in the macOS Logs folder.`,
      buttons: ["Retry", "Quit"], defaultId: 0, cancelId: 1 });
    creating = false;
    if (result.response === 0) return createWindow();
    app.quit();
  } finally { creating = false; }
}

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", () => createWindow());
  app.whenReady().then(createWindow);
  app.on("activate", () => createWindow());
  app.on("window-all-closed", () => { if (process.platform !== "darwin") app.quit(); });
  app.on("before-quit", (event) => {
    if (quitting) return;
    event.preventDefault();
    quitting = true;
    stopBackend().finally(() => { if (logStream) logStream.end(); app.quit(); });
  });
}
