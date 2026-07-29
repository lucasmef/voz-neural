"use strict";

const path = require("path");
const WebSocket = require(
  "/Applications/Adobe UXP Developer Tools/" +
    "Adobe UXP Developer Tools.app/Contents/Resources/" +
    "app.asar/node_modules/ws"
);

const SERVICE_URL = "ws://127.0.0.1:14001/socket/cli";
const ACTION = process.argv[2] || "clients";
const PLUGIN_DIR = process.argv[3]
  ? path.resolve(process.argv[3])
  : path.resolve(__dirname, "../soundlift_uxp_plugin");

let nextRequestId = 0;
let completedConnection = false;
let auditionClient = null;
const pending = new Map();
const receivedClients = [];

function sendWithReply(socket, clientId, message) {
  const requestId = ++nextRequestId;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      pending.delete(requestId);
      reject(new Error(`Timeout no pedido ${requestId}: ${message.action}`));
    }, 15000);

    pending.set(requestId, {
      resolve(value) {
        clearTimeout(timer);
        resolve(value);
      },
      reject(error) {
        clearTimeout(timer);
        reject(error);
      },
    });

    socket.send(
      JSON.stringify({
        command: "proxy",
        clientId,
        message,
        requestId,
      })
    );
  });
}

function isAudition(app) {
  const haystack = [
    app && app.appId,
    app && app.appName,
    app && app.name,
  ]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
  return haystack.includes("audition");
}

async function runAction(socket) {
  if (ACTION === "clients") {
    process.stdout.write(
      `${JSON.stringify({ clients: receivedClients }, null, 2)}\n`
    );
    socket.close();
    return;
  }

  if (!auditionClient) {
    throw new Error("O Audition não apareceu entre os hosts UXP conectados.");
  }

  const app = auditionClient.app;
  const provider = { type: "disk", path: PLUGIN_DIR };

  if (ACTION === "validate" || ACTION === "load") {
    const validation = await sendWithReply(socket, auditionClient.id, {
      command: "Plugin",
      action: "validate",
      params: { provider },
    });
    process.stdout.write(
      `${JSON.stringify({ step: "validate", app, validation }, null, 2)}\n`
    );
  }

  if (ACTION === "load") {
    const loaded = await sendWithReply(socket, auditionClient.id, {
      command: "Plugin",
      action: "load",
      params: { provider },
      breakOnStart: false,
      isPlaygroundPlugin: false,
    });
    process.stdout.write(
      `${JSON.stringify({ step: "load", app, loaded }, null, 2)}\n`
    );
  }

  socket.close();
}

const socket = new WebSocket(SERVICE_URL);

socket.on("message", (raw) => {
  let data;
  try {
    data = JSON.parse(String(raw));
  } catch (error) {
    process.stderr.write(`Mensagem UXP inválida: ${String(raw)}\n`);
    return;
  }

  if (data.command === "didAddRuntimeClient") {
    receivedClients.push(data);
    if (isAudition(data.app)) auditionClient = data;
    return;
  }

  if (data.command === "didCompleteConnection") {
    completedConnection = true;
    runAction(socket).catch((error) => {
      process.stderr.write(`${error.stack || error}\n`);
      socket.close();
      process.exitCode = 1;
    });
    return;
  }

  if (data.command === "reply" && data.requestId) {
    const callback = pending.get(data.requestId);
    if (!callback) return;
    pending.delete(data.requestId);
    if (data.error || data.success === false) {
      callback.reject(new Error(data.error || JSON.stringify(data)));
    } else {
      callback.resolve(data);
    }
  }
});

socket.on("error", (error) => {
  process.stderr.write(`Falha no serviço UXP: ${error.message}\n`);
  process.exitCode = 1;
});

socket.on("close", () => {
  if (!completedConnection && !process.exitCode) {
    process.stderr.write("A conexão UXP fechou antes da inicialização.\n");
    process.exitCode = 1;
  }
});
