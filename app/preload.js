"use strict";
// Phantom preload. The renderer talks to the local backend over fetch()/WebSocket.
// contextIsolation is on and nodeIntegration is off, so we expose only the
// per-launch auth token (passed in via --phantom-token) through a minimal bridge.
const { contextBridge } = require("electron");

const arg = (process.argv || []).find((a) => a.startsWith("--phantom-token="));
const token = arg ? arg.slice("--phantom-token=".length) : "";

contextBridge.exposeInMainWorld("phantom", { token });
