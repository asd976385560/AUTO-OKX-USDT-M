#!/usr/bin/env node

/**
 * Keep the OpenClaw CLI and its Gateway request in one Node process while a
 * parent supervisor asks for cancellation through an identity-bound file.
 *
 * Windows TerminateProcess/taskkill cannot deliver Node's SIGTERM event.  The
 * OpenClaw agent CLI, however, already handles that event by sending
 * chat.abort through the active Gateway connection.  This adapter watches an
 * atomic control file and emits SIGTERM inside the same process, so OpenClaw's
 * built-in handler retains the originating connection identity.  The parent
 * still owns the bounded hard-kill fallback and terminal verification.
 */

import {
  mkdirSync,
  readFileSync,
  renameSync,
  writeFileSync,
} from "node:fs";
import { dirname, resolve } from "node:path";
import { pathToFileURL } from "node:url";

const CONTROL_POLL_MS = 100;
const CONTROL_SCHEMA_VERSION = 1;
const RECEIPT_SCHEMA = "okx.openclaw-agent-control-receipt.v1";
const CONTROL_ID_RE = /^[A-Za-z0-9_.:-]{8,128}$/u;
const GATEWAY_TERMINAL_ERROR_MARKER =
  "GatewayClientRequestError: FallbackSummaryError: All models failed";

function fail(message) {
  throw new Error(message);
}

function requireValue(argv, index, flag) {
  const value = argv[index + 1];
  if (value === undefined || value === "--" || value.startsWith("--")) {
    fail(`${flag} requires a value`);
  }
  return value;
}

function parseArgs(argv) {
  const options = {
    openclawMjs: undefined,
    controlFile: undefined,
    receiptFile: undefined,
    controlId: undefined,
    selfTest: false,
    selfTestTerminalError: false,
    passthrough: [],
  };
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    if (arg === "--") {
      options.passthrough = argv.slice(index + 1);
      break;
    }
    if (arg === "--self-test") {
      options.selfTest = true;
      continue;
    }
    if (arg === "--self-test-terminal-error") {
      options.selfTestTerminalError = true;
      continue;
    }
    const valueFlags = new Map([
      ["--openclaw-mjs", "openclawMjs"],
      ["--abort-control-file", "controlFile"],
      ["--abort-receipt-file", "receiptFile"],
      ["--control-id", "controlId"],
    ]);
    const key = valueFlags.get(arg);
    if (!key) {
      fail(`unknown adapter option: ${arg}`);
    }
    options[key] = requireValue(argv, index, arg);
    index += 1;
  }

  const controlValues = [
    options.controlFile,
    options.receiptFile,
    options.controlId,
  ].filter((value) => value !== undefined);
  if (controlValues.length !== 0 && controlValues.length !== 3) {
    fail("abort control file, receipt file, and control id must be supplied together");
  }
  if (options.controlId && !CONTROL_ID_RE.test(options.controlId)) {
    fail("control id has an invalid shape");
  }
  if (options.selfTest || options.selfTestTerminalError) {
    if (process.env.OKX_AGENT_ADAPTER_SELF_TEST !== "1") {
      fail("self-test mode requires OKX_AGENT_ADAPTER_SELF_TEST=1");
    }
    if (controlValues.length !== 3) {
      fail("self-test mode requires the complete abort-control tuple");
    }
    return options;
  }
  if (!options.openclawMjs) {
    fail("--openclaw-mjs is required");
  }
  if (options.passthrough[0] !== "agent") {
    fail("adapter only accepts the OpenClaw agent command");
  }
  return options;
}

function shortError(error) {
  const text = error instanceof Error
    ? `${error.name}: ${error.message}`
    : String(error);
  return text.slice(0, 500);
}

function writeJsonAtomic(path, payload) {
  const target = resolve(path);
  mkdirSync(dirname(target), { recursive: true });
  const temp = `${target}.${process.pid}.${Date.now()}.tmp`;
  writeFileSync(temp, JSON.stringify(payload, null, 2), {
    encoding: "utf8",
    flag: "wx",
  });
  renameSync(temp, target);
}

async function main() {
  const options = parseArgs(process.argv.slice(2));
  const state = {
    schema: RECEIPT_SCHEMA,
    wrapper_pid: process.pid,
    control_id: options.controlId ?? null,
    control_configured: Boolean(options.controlFile),
    started_at: new Date().toISOString(),
    control_request_observed: false,
    signal_delivered: false,
    signal_delivery_listener_count: 0,
    command_completed: false,
    gateway_terminal_error_observed: false,
  };
  let receiptWritten = false;
  let watcher;
  let lastControlRejection;
  let stderrTail = "";
  const originalStderrWrite = process.stderr.write.bind(process.stderr);
  process.stderr.write = (chunk, ...args) => {
    const value = Buffer.isBuffer(chunk) ? chunk.toString("utf8") : String(chunk);
    stderrTail = `${stderrTail}${value}`.slice(-4096);
    if (stderrTail.includes(GATEWAY_TERMINAL_ERROR_MARKER)) {
      state.gateway_terminal_error_observed = true;
      state.gateway_terminal_error_marker = "all_models_failed";
    }
    return originalStderrWrite(chunk, ...args);
  };

  const writeReceipt = (exitCode, phase) => {
    if (!options.receiptFile || receiptWritten) {
      return;
    }
    try {
      writeJsonAtomic(options.receiptFile, {
        ...state,
        phase,
        exit_code: Number.isInteger(exitCode) ? exitCode : null,
        finished_at: new Date().toISOString(),
      });
      receiptWritten = true;
    } catch (error) {
      process.stderr.write(
        `[okx-agent-adapter] receipt write failed: ${shortError(error)}\n`,
      );
    }
  };

  process.on("exit", (code) => {
    if (watcher) {
      clearInterval(watcher);
    }
    writeReceipt(code, state.signal_delivered ? "signal_exit" : "process_exit");
  });

  const rejectControl = (reason) => {
    state.control_rejected_reason = reason;
    if (reason !== lastControlRejection) {
      lastControlRejection = reason;
      process.stderr.write(`[okx-agent-adapter] control rejected: ${reason}\n`);
    }
  };

  const pollControl = () => {
    if (!options.controlFile || state.signal_delivered) {
      return;
    }
    let payload;
    try {
      payload = JSON.parse(readFileSync(options.controlFile, "utf8"));
    } catch (error) {
      if (error?.code === "ENOENT") {
        return;
      }
      rejectControl(shortError(error));
      return;
    }
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
      rejectControl("control payload must be an object");
      return;
    }
    if (payload.schema_version !== CONTROL_SCHEMA_VERSION) {
      rejectControl("control schema mismatch");
      return;
    }
    if (payload.control_id !== options.controlId) {
      rejectControl("control id mismatch");
      return;
    }
    if (payload.action !== "abort" || payload.signal !== "SIGTERM") {
      rejectControl("unsupported control action");
      return;
    }

    state.control_request_observed = true;
    state.control_requested_at = typeof payload.requested_at === "string"
      ? payload.requested_at.slice(0, 64)
      : null;
    state.control_reason = typeof payload.reason === "string"
      ? payload.reason.slice(0, 200)
      : null;
    const listenerCount = process.listenerCount("SIGTERM");
    state.signal_delivery_listener_count = listenerCount;
    if (listenerCount < 1) {
      return;
    }

    state.signal_delivered = true;
    state.signal_delivered_at = new Date().toISOString();
    if (watcher) {
      clearInterval(watcher);
      watcher = undefined;
    }
    process.emit("SIGTERM");
  };

  if (options.controlFile) {
    watcher = setInterval(pollControl, CONTROL_POLL_MS);
    if (!options.selfTest) {
      watcher.unref?.();
    }
    pollControl();
  }

  if (options.selfTest) {
    process.on("SIGTERM", () => process.exit(143));
    await new Promise(() => {});
    return;
  }
  if (options.selfTestTerminalError) {
    process.stderr.write(
      `${GATEWAY_TERMINAL_ERROR_MARKER} (self-test)\n`,
    );
    process.exit(1);
  }

  const openclawMjs = resolve(options.openclawMjs);
  process.argv = [process.execPath, openclawMjs, ...options.passthrough];
  await import(pathToFileURL(openclawMjs).href);
  state.command_completed = true;
  if (watcher) {
    clearInterval(watcher);
    watcher = undefined;
  }
  const exitCode = Number.isInteger(process.exitCode) ? process.exitCode : 0;
  writeReceipt(exitCode, "completed");
}

main().catch((error) => {
  process.stderr.write(`[okx-agent-adapter] ${shortError(error)}\n`);
  process.exitCode = 1;
});
