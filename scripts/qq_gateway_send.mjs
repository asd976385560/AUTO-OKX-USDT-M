// QQ transport through the resident OpenClaw Gateway. Called only by qq_push_raw.py.
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { randomUUID } from "node:crypto";
import { pathToFileURL } from "node:url";

export function connectionOptions(config, timeoutMs, env = process.env) {
  const gateway = config.gateway;
  if (gateway?.mode !== "local") throw new Error("QQ transport requires the configured local gateway");
  const port = gateway.port ?? 18789;
  if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error("Invalid local gateway port");
  const authMode = gateway.auth?.mode;
  if (!["token", "password"].includes(authMode)) throw new Error("Local gateway shared authentication required");
  const credential = env[authMode === "token" ? "OPENCLAW_GATEWAY_TOKEN" : "OPENCLAW_GATEWAY_PASSWORD"] || gateway.auth[authMode];
  if (typeof credential !== "string" || !credential || credential.includes("${")) throw new Error("Local gateway credential unresolved");
  return { config, localPortOverride: port, [authMode]: credential,
    clientName: "gateway-client", mode: "backend", requireLocalBackendSharedAuth: true,
    timeoutMs: Math.min(45000, Math.max(1000, Number(timeoutMs) || 45000)) };
}

export async function performSend(input, config, callGateway, env = process.env) {
  if (typeof input.content !== "string" || !input.content.trim()) throw new Error("Empty QQ content");
  if (typeof input.target !== "string" || !/^(?:qqbot:)?(?:group|c2c):[A-Za-z0-9_-]+$/.test(input.target)) throw new Error("Invalid QQ target");
  const options = connectionOptions(config, input.timeoutMs, env);
  const agentId = config.agents?.defaults?.systemAgent?.agentId;
  if (typeof agentId !== "string" || !agentId.trim()) throw new Error("Configured system agent owner required for QQ delivery");
  let result;
  try {
    result = await callGateway({ ...options, method: "send", params: {
      channel: "qqbot", accountId: "default", agentId, to: input.target,
      message: input.content, idempotencyKey: randomUUID(),
    } });
    if (typeof result?.messageId !== "string" || !result.messageId.trim()
        || ["failed", "partial_failed", "suppressed"].includes(result.deliveryStatus)) {
      throw new Error("Gateway returned no confirmed QQ messageId");
    }
  } catch (cause) {
    const error = new Error("QQ gateway delivery outcome unknown: " + String(cause?.message ?? cause));
    error.uncertainDelivery = true;
    throw error;
  }
  return { action: "send", channel: "qqbot", via: "gateway", messageId: result.messageId };
}

async function main() {
  let config;
  try {
    const input = JSON.parse(fs.readFileSync(0, "utf8"));
    const configPath = process.env.OPENCLAW_CONFIG_PATH || path.join(os.homedir(), ".openclaw", "openclaw.json");
    config = JSON.parse(fs.readFileSync(configPath, "utf8"));
    connectionOptions(config, input.timeoutMs);
    const runtimePath = path.join(path.dirname(input.openclawMjs), "dist", "message.gateway.runtime.js");
    const { callGatewayLeastPrivilege } = await import(pathToFileURL(runtimePath).href);
    const result = await performSend(input, config, callGatewayLeastPrivilege);
    process.stdout.write(JSON.stringify(result) + "\n");
    process.exit(0);
  } catch (error) {
    let message = String(error?.message ?? error);
    for (const credential of [config?.gateway?.auth?.token, config?.gateway?.auth?.password,
      process.env.OPENCLAW_GATEWAY_TOKEN, process.env.OPENCLAW_GATEWAY_PASSWORD]) {
      if (typeof credential === "string" && credential) message = message.split(credential).join("[redacted]");
    }
    process.stderr.write(JSON.stringify({ error: message, uncertainDelivery: error.uncertainDelivery === true }) + "\n");
    process.exit(error.uncertainDelivery ? 3 : 1);
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) await main();
