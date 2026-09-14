import test from "node:test";
import assert from "node:assert/strict";
import { connectionOptions, performSend } from "../scripts/qq_gateway_send.mjs";
const config = { gateway: { mode: "local", port: 18789, auth: { mode: "token", token: "fixture" } }, agents: { defaults: { systemAgent: { agentId: "fixture-owner" } } } };
const input = { content: "完整报告\n".repeat(1000), target: "group:PUBLIC_GROUP_OPENID", timeoutMs: 45000 };

test("one native send, unchanged complete content and target, receipt required", async () => {
  const calls = [];
  const result = await performSend(input, config, async (options) => {
    calls.push(options); return { messageId: "confirmed-id" };
  }, {});
  assert.equal(calls.length, 1);
  assert.equal(calls[0].method, "send");
  assert.equal(calls[0].params.message, input.content);
  assert.equal(calls[0].params.to, input.target);
  assert.equal(calls[0].params.channel, "qqbot");
  assert.equal(calls[0].params.agentId, "fixture-owner");
  assert.equal(calls[0].requireLocalBackendSharedAuth, true);
  assert.equal(calls[0].scopes, undefined);
  assert.equal(result.messageId, "confirmed-id");
});

for (const response of [{}, { messageId: "" }, { messageId: "x", deliveryStatus: "partial_failed" }]) {
  test("missing or failed receipt remains uncertain " + JSON.stringify(response), async () => {
    let calls = 0;
    await assert.rejects(performSend(input, config, async () => { calls++; return response; }, {}),
      error => error.uncertainDelivery === true);
    assert.equal(calls, 1);
  });
}
test("timeout after invocation is uncertain and never retries", async () => {
  let calls = 0;
  await assert.rejects(performSend(input, config, async () => { calls++; throw new Error("timeout"); }, {}),
    error => error.uncertainDelivery === true);
  assert.equal(calls, 1);
});
test("remote or unresolved authentication fails before the send callback", async () => {
  for (const gateway of [{...config.gateway, mode:"remote"}, {...config.gateway, auth:{mode:"token",token:{source:"env"}}}]) {
    let calls=0;
    await assert.rejects(performSend(input,{gateway},async()=>{calls++;return{};},{}));
    assert.equal(calls,0);
  }
  assert.equal(connectionOptions(config, 999999, {}).timeoutMs, 45000);
});
test("multi-agent delivery requires the configured owner before invoking send", async () => {
  let calls=0;
  await assert.rejects(performSend(input,{gateway:config.gateway},async()=>{calls++;return{};},{}), /system agent owner/);
  assert.equal(calls,0);
});
