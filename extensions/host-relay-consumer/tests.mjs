import { test } from "node:test";
import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { PassThrough } from "node:stream";
import childProcess from "node:child_process";
import { syncBuiltinESMExports } from "node:module";
import { mkdtempSync, mkdirSync, rmSync, cpSync, symlinkSync } from "node:fs";
import { homedir, tmpdir } from "node:os";
import { join, resolve } from "node:path";
import {
  NativeRelay,
  AGENT,
  AGENT_FILE,
  CWD,
  EVENTS,
  verifyContract,
  errorCode,
} from "./model.ts";
import extension from "./index.ts";

// Test process only: synthetic root-host fixtures must not inherit the worker guard.
delete process.env.PI_SUBAGENT_CHILD;

const request = {
  stem: `${"a".repeat(32)}.1.${"b".repeat(64)}`,
  jobId: "d".repeat(32),
  prompt: "SYNTHETIC ONLY",
  maxOutputBytes: 4096 - 512,
  timeoutMs: 1000,
};
const ctx = {
  model: { provider: "synthetic", id: "model", contextWindow: 400000 },
  modelRegistry: { getAvailable: () => [ctx.model] },
};
const identity = "synthetic/model";
const digest = "c".repeat(64);
const runtime =
  "C:/packages/pi-subagents/src/runs/shared/subagent-prompt-runtime.ts";
function contract() {
  return {
    ok: true,
    contract: {
      version: 3,
      protocol: { packageVersion: "0.67.0" },
      agent: { name: AGENT, source: "project", filePath: AGENT_FILE },
      model: `${identity}:off`,
      modelCandidates: [`${identity}:off`],
      context: "fresh",
      systemPromptMode: "replace",
      inheritProjectContext: false,
      inheritGlobalContext: false,
      inheritSkills: false,
      skills: { requested: [], resolved: [], missing: [] },
      intercomBridge: { mode: "off", active: false },
      roots: { cwd: CWD },
      tools: {
        explicitAllowlist: true,
        fanoutAuthorized: false,
        requestedBuiltin: [],
        declaredBuiltin: [],
        effectiveAllowlist: [],
        requiredChildTools: [],
        internalTools: [],
        mcp: [],
        effectiveMcpTools: [],
        disableAmbientExtensions: true,
        configuredExtensions: [],
        toolExtensionPaths: [],
        runtimeExtensions: [runtime],
        extensionArgs: [runtime],
      },
      diagnostics: [],
      launchContractDigest: digest,
    },
  };
}
function bus() {
  const emitter = new EventEmitter();
  return {
    on: (name, fn) => {
      emitter.on(name, fn);
      return () => emitter.off(name, fn);
    },
    emit: (name, value) => {
      emitter.emit(name, value);
    },
    emitter,
  };
}
const tick = () => new Promise((resolve) => setImmediate(resolve));
function harness(preflight = async () => contract()) {
  const events = bus();
  const sent = [];
  const cancels = [];
  events.on(EVENTS.request, (value) => sent.push(value));
  events.on(EVENTS.cancel, (value) => cancels.push(value));
  const native = new NativeRelay(events, preflight);
  const controller = new AbortController();
  return {
    events,
    native,
    controller,
    sent,
    cancels,
    start: (r = request) => native.generate(ctx, r, controller),
    terminal: (overrides = {}, binding = sent[0]) =>
      events.emit(EVENTS.response, {
        requestId: binding.requestId,
        ownerRunId: binding.ownerRunId,
        nodeId: binding.nodeId,
        status: "completed",
        agent: AGENT,
        model: `${identity}:off`,
        runId: "native-run",
        launchContractDigest: digest,
        usage: { toolCalls: 0 },
        result: {
          kind: "text",
          text: JSON.stringify({ job_id: request.jobId, ok: true }),
        },
        ...overrides,
      }),
  };
}

test("model.ts verifies effective agent, model/fallback, context, tools, skills, MCP and extensions", () => {
  assert.equal(verifyContract(contract(), identity), digest);
  const mutations = [
    (c) => {
      c.agent.filePath = "foreign.md";
    },
    (c) => {
      c.agent.source = "runtime";
    },
    (c) => {
      c.model = "other/model:off";
    },
    (c) => {
      c.modelCandidates.push("other/model:off");
    },
    (c) => {
      c.tools.explicitAllowlist = false;
    },
    (c) => {
      c.tools.effectiveAllowlist.push("read");
    },
    (c) => {
      c.tools.internalTools.push("structured_output");
    },
    (c) => {
      c.tools.mcp.push({});
    },
    (c) => {
      c.tools.fanoutAuthorized = true;
    },
    (c) => {
      c.tools.disableAmbientExtensions = false;
    },
    (c) => {
      c.tools.configuredExtensions.push("evil.ts");
    },
    (c) => {
      c.tools.runtimeExtensions.push("evil.ts");
    },
    (c) => {
      c.skills.resolved.push({ name: "skill" });
    },
    (c) => {
      c.inheritSkills = true;
    },
    (c) => {
      c.context = "fork";
    },
    (c) => {
      c.inheritGlobalContext = true;
    },
    (c) => {
      c.intercomBridge.active = true;
    },
    (c) => {
      c.roots.outputPath = "output.md";
    },
    (c) => {
      c.diagnostics.push({ severity: "host-required" });
    },
  ];
  for (const mutate of mutations) {
    const value = contract();
    mutate(value.contract);
    assert.throws(() => verifyContract(value, identity));
  }
});

test("model.ts emits one fresh explicit text delegation, no tools/bridge/skills/artifacts", async () => {
  let input;
  const h = harness(async (value) => {
    input = value;
    return contract();
  });
  const pending = h.start();
  await tick();
  assert.equal(input.task, request.prompt);
  assert.equal(input.model, identity);
  const sent = h.sent[0];
  assert.equal(sent.model, identity);
  assert.equal(sent.context, "fresh");
  assert.equal(sent.cwd, CWD);
  assert.deepEqual(sent.toolBudget, { hard: 0, block: "*" });
  assert.equal(sent.skill, false);
  assert.equal(sent.artifacts, false);
  assert.deepEqual(sent.intercomBridge, { mode: "off" });
  assert.deepEqual(sent.result, { kind: "text" });
  h.terminal();
  assert.deepEqual(await pending, {
    stem: request.stem,
    output: { job_id: request.jobId, ok: true },
  });
  for (const name of [EVENTS.response, EVENTS.started, EVENTS.update])
    assert.equal(h.events.emitter.listenerCount(name), 0);
});

test("model.ts validates strict JSON, job binding, UTF-8 bytes, terminal identity and digest", async () => {
  for (const [change, code] of [
    [
      { result: { kind: "text", text: "```json\n{}\n```" } },
      "relay_output_json",
    ],
    [{ result: { kind: "text", text: "[]" } }, "relay_output_binding"],
    [
      { result: { kind: "text", text: '{"job_id":"foreign"}' } },
      "relay_output_binding",
    ],
    [
      { result: { kind: "text", text: "Ã¤Â¸Â­".repeat(1400) } },
      "relay_output_oversize",
    ],
    [
      { result: { kind: "structured", value: {} } },
      "relay_native_result_invalid",
    ],
    [{ model: "other/model" }, "relay_native_contract_mismatch"],
    [{ usage: { toolCalls: 1 } }, "relay_native_contract_mismatch"],
    [{ launchContractDigest: "changed" }, "relay_native_contract_mismatch"],
    [
      { status: "failed", error: "SECRET RAW PROVIDER ERROR" },
      "relay_native_failed",
    ],
  ]) {
    const h = harness();
    const pending = h.start();
    await tick();
    h.terminal(change);
    assert.deepEqual(await pending, { stem: request.stem, failed: code });
  }
});

test("model.ts cancellation waits for correlated terminal and retains single concurrency", async () => {
  const h = harness();
  let settled = false;
  const pending = h.start().then((value) => {
    settled = true;
    return value;
  });
  await tick();
  h.controller.abort();
  await tick();
  assert.equal(settled, false);
  assert.equal(h.cancels.length, 1);
  assert.equal(
    (await h.native.generate(ctx, request, new AbortController())).failed,
    "relay_already_running",
  );
  h.terminal({ requestId: "foreign" });
  h.terminal({ ownerRunId: "foreign" });
  h.terminal({ nodeId: "foreign" });
  await tick();
  assert.equal(settled, false);
  h.events.emit(EVENTS.started, h.sent[0]);
  assert.equal(h.cancels.length, 2);
  h.terminal({ status: "cancelled" });
  assert.equal((await pending).failed, "relay_cancelled");
  h.terminal();
  assert.equal(h.events.emitter.listenerCount(EVENTS.response), 0);
  const next = h.native.generate(ctx, request, new AbortController());
  await tick();
  h.terminal();
  await tick(); // Old tuple cannot settle a new attempt.
  assert.equal(h.events.emitter.listenerCount(EVENTS.response), 1);
  h.terminal({}, h.sent[1]);
  assert.ok((await next).output);
});

test("model.ts timeout and unexpected tool/model updates cancel but never invent terminal", async () => {
  for (const mode of ["timeout", "tool", "model"]) {
    const h = harness();
    let settled = false;
    const pending = h
      .start({ ...request, timeoutMs: mode === "timeout" ? 20 : 1000 })
      .then((value) => {
        settled = true;
        return value;
      });
    await tick();
    if (mode === "timeout")
      await new Promise((resolve) => setTimeout(resolve, 35));
    else
      h.events.emit(EVENTS.update, {
        ...h.sent[0],
        runId: "native-run",
        ...(mode === "tool"
          ? { toolCount: 1, recentOutput: "SECRET" }
          : { model: "foreign/model" }),
      });
    assert.equal(settled, false);
    assert.equal(h.cancels.length, 1);
    h.terminal({ runId: "foreign-run" });
    if (mode !== "timeout") {
      await tick();
      assert.equal(settled, false);
    }
    h.terminal({ status: "cancelled" });
    assert.equal(
      (await pending).failed,
      mode === "timeout" ? "relay_timeout" : "relay_native_contract_mismatch",
    );
  }
});

test("model.ts cancellation during preflight prevents dispatch; invalid bounds/model fail locally", async () => {
  let release;
  const h = harness(
    () =>
      new Promise((resolve) => {
        release = resolve;
      }),
  );
  const pending = h.start();
  h.controller.abort();
  release(contract());
  assert.equal((await pending).failed, "relay_cancelled");
  assert.equal(h.sent.length, 0);
  for (const change of [
    { maxOutputBytes: 3583 },
    { maxOutputBytes: 8388609 },
    { timeoutMs: 900001 },
    { timeoutMs: 0 },
    { prompt: "Ã¤Â¸Â­".repeat(65537) },
  ]) {
    const f = harness();
    assert.ok((await f.start({ ...request, ...change })).failed);
    assert.equal(f.sent.length, 0);
  }
  const f = harness();
  assert.equal(
    (
      await f.native.generate(
        { model: { ...ctx.model, contextWindow: 200000 } },
        request,
        new AbortController(),
      )
    ).failed,
    "relay_context_limit_invalid",
  );
  assert.equal(f.sent.length, 0);
});

test("model.ts ambiguous dispatch exceptions stay busy until terminal; diagnostics are allowlisted", async () => {
  const h = harness();
  h.events.on(EVENTS.request, () => {
    throw new Error("SECRET");
  });
  const pending = h.start();
  await tick();
  assert.equal((await h.start()).failed, "relay_already_running");
  h.terminal({ status: "cancelled" });
  assert.equal((await pending).failed, "relay_native_dispatch_error");
  for (const value of [
    new Error("relay_SECRET"),
    new Error("private raw"),
    new Proxy(
      {},
      {
        getPrototypeOf() {
          throw Error("secret");
        },
      },
    ),
  ])
    assert.equal(errorCode(value), "relay_native_failed");
});

function host(native) {
  const commands = new Map();
  const hooks = new Map();
  const notices = [];
  const pi = {
    events: bus(),
    registerCommand: (key, value) => commands.set(key, value),
    on: (key, value) => hooks.set(key, value),
  };
  extension(pi, async () => native);
  const context = {
    ...ctx,
    mode: "tui",
    ui: { notify: (value) => notices.push(value) },
  };
  return {
    commands,
    hooks,
    notices,
    context,
    call: (key, text = "") => commands.get(key).handler(text, context),
  };
}

test("index.ts default OFF and no-network preflight; obsolete probe removed and host guards preserved", async () => {
  const h = host({ readiness: async () => ({ identity }) });
  await h.call("relay-status");
  assert.deepEqual(JSON.parse(h.notices.pop()), {
    code: "relay_off",
    running: false,
    busy: false,
    published: 0,
    preflightVerified: false,
    nativeAcceptance: "not_verified",
  });
  assert.equal(h.commands.has("relay-probe"), false);
  await h.call("relay-start", "{}");
  assert.equal(h.notices.pop(), "relay_preflight_required");
  await h.call("relay-preflight");
  assert.equal(h.notices.pop(), "relay_preflight_ok_no_network");
  await h.hooks.get("model_select")();
  await h.call("relay-status");
  assert.equal(JSON.parse(h.notices.pop()).preflightVerified, false);
  for (const mode of ["print", "json"]) {
    h.context.mode = mode;
    await h.call("relay-preflight");
    assert.equal(h.notices.pop(), "relay_host_forbidden");
  }
  h.context.mode = "tui";
  const old = process.env.PI_SUBAGENT_CHILD;
  try {
    process.env.PI_SUBAGENT_CHILD = "1";
    await h.call("relay-preflight");
    assert.equal(h.notices.pop(), "relay_host_forbidden");
  } finally {
    if (old === undefined) delete process.env.PI_SUBAGENT_CHILD;
    else process.env.PI_SUBAGENT_CHILD = old;
  }
});

test("index.ts session lifecycle revokes in-flight readiness and sanitizes errors", async () => {
  for (const lifecycle of [
    "session_before_switch",
    "session_shutdown",
    "model_select",
  ]) {
    let release;
    const h = host({
      readiness: () =>
        new Promise((resolve) => {
          release = resolve;
        }),
    });
    const pending = h.call("relay-preflight");
    await tick();
    await h.hooks.get(lifecycle)();
    release({ identity });
    await pending;
    assert.equal(h.notices.pop(), "relay_cancelled");
    await h.call("relay-status");
    assert.equal(JSON.parse(h.notices.pop()).preflightVerified, false);
  }
  const h = host({
    readiness: async () => {
      throw new Error("SECRET");
    },
  });
  await h.call("relay-preflight");
  assert.equal(h.notices.pop(), "relay_native_failed");
  await h.call("relay-status");
  assert.ok(!h.notices.pop().includes("SECRET"));
});

test("public pi-subagents 0.67.0 preflight discovers the dedicated agent in an isolated synthetic host", () => {
  const root = mkdtempSync(join(tmpdir(), "relay-native-preflight-"));
  const sdkRoot = join(
    homedir(),
    "AppData/Roaming/npm/node_modules/@earendil-works/pi-coding-agent",
  );
  const installedRoot = join(homedir(), ".pi/agent/npm");
  const consumer = join(root, "consumer");
  const modules = join(root, ".pi/agent/npm/node_modules");
  mkdirSync(modules, { recursive: true });
  symlinkSync(
    join(installedRoot, "node_modules/pi-subagents"),
    join(modules, "pi-subagents"),
    "junction",
  );
  mkdirSync(consumer, { recursive: true });
  mkdirSync(join(consumer, ".pi/agents"), { recursive: true });
  cpSync(AGENT_FILE, join(consumer, ".pi/agents/host-relay-json.md"));
  cpSync(join(CWD, "model.ts"), join(consumer, "model.ts"));
  const alias = Object.fromEntries(
    ["pi-coding-agent", "pi-ai", "pi-agent-core", "pi-tui"].map((name) => [
      `@earendil-works/${name}`,
      name === "pi-coding-agent"
        ? join(sdkRoot, "dist/index.js")
        : join(sdkRoot, `node_modules/@earendil-works/${name}/dist/index.js`),
    ]),
  );
  const script = `
    import { createRequire } from 'node:module';
    import assert from 'node:assert/strict';
    import { writeFileSync, readFileSync, mkdirSync } from 'node:fs';
    import { join } from 'node:path';
    const require = createRequire(${JSON.stringify(join(sdkRoot, "package.json"))});
    const { createJiti } = require('jiti');
    const jiti = createJiti(${JSON.stringify(join(consumer, "loader.mjs"))}, { alias: ${JSON.stringify(alias)}, moduleCache: false });
    globalThis.fetch = () => { throw Error('NETWORK FORBIDDEN'); };
    const model = await jiti.import(${JSON.stringify(join(consumer, "model.ts"))});
    const preflight = await model.loadPreflight();
    const input = { agent: model.AGENT, cwd: model.CWD, context: 'fresh', model: 'synthetic/model',
      task: 'SYNTHETIC ONLY', thinking: 'off', skill: false, artifacts: false,
      intercomBridge: {mode:'off'}, availableModels: [{provider:'synthetic',id:'model',contextWindow:400000}] };
    const result = await preflight(input);
    if (!result.ok) throw Error(JSON.stringify(result));
    assert.throws(() => model.verifyContract(result, 'synthetic/model'), /relay_host_validation_pending/);
    console.log('PRODUCTION_PATH_GATE_CLOSED');
    // Test-only caller-selected path; NOT an assertion about the native host's actual root.
    input.sessionRoot = join(process.env.HOME, 'synthetic-child-root');
    const testContract = await preflight(input);
    assert.equal(model.verifyContract(testContract, 'synthetic/model'), testContract.contract.launchContractDigest);
    console.log('PUBLIC_PREFLIGHT_OK', JSON.stringify({tools:testContract.contract.tools.effectiveAllowlist,
      candidates:testContract.contract.modelCandidates, ambient:testContract.contract.tools.disableAmbientExtensions}));
    mkdirSync(process.env.PI_CODING_AGENT_DIR, {recursive:true});
    writeFileSync(join(process.env.PI_CODING_AGENT_DIR,'settings.json'), JSON.stringify({ subagents: {
      agentOverrides: { 'host-relay-json': { tools: ['read'] } } } }));
    const overridden = await preflight(input);
    assert.throws(() => model.verifyContract(overridden,'synthetic/model'));
    console.log('EFFECTIVE_OVERRIDE_REJECTED');
    // NATIVE-MODEL-SNAPSHOT-01: static configuration; only registry visibility differs.
    const settings = join(process.env.PI_CODING_AGENT_DIR, 'settings.json');
    const fallbackConfig = JSON.stringify({ subagents: { agentOverrides: {
      'host-relay-json': { fallbackModels: ['synthetic/backup'] }
    } } });
    writeFileSync(settings, fallbackConfig);
    const models = [
      { provider: 'synthetic', id: 'model', contextWindow: 400000, reasoning: false },
      { provider: 'synthetic', id: 'backup', contextWindow: 400000, reasoning: false }
    ];
    const hidden = await preflight({...input, availableModels: [models[0]]});
    assert.equal(model.verifyContract(hidden, 'synthetic/model'), hidden.contract.launchContractDigest);
    const visible = await preflight({...input, availableModels: models});
    assert.throws(() => model.verifyContract(visible, 'synthetic/model'), /relay_model_mismatch/);
    let snapshot;
    const relay = new model.NativeRelay({ emit() { throw Error('NO DELEGATION'); } }, async value => {
      snapshot = value.availableModels;
      return preflight({...value, sessionRoot: input.sessionRoot}); // Test-only root.
    });
    await assert.rejects(relay.readiness({ model: models[0], modelRegistry: { getAvailable: () => models } }),
      /relay_model_mismatch/);
    assert.deepEqual(snapshot.map(m => m.provider + '/' + m.id), ['synthetic/model', 'synthetic/backup']);
    assert.equal(readFileSync(settings, 'utf8'), fallbackConfig);
    console.log('NATIVE_MODEL_SNAPSHOT_REGRESSION_PASSED');

  `;
  try {
    const result = childProcess.spawnSync(
      process.execPath,
      ["--input-type=module", "-e", script],
      {
        cwd: root,
        encoding: "utf8",
        timeout: 60000,
        env: {
          SystemRoot: process.env.SystemRoot,
          WINDIR: process.env.WINDIR,
          TEMP: root,
          TMP: root,
          HOME: root,
          USERPROFILE: root,
          PI_CODING_AGENT_DIR: join(root, "agent"),
          PI_SUBAGENTS_PI_CODING_AGENT_PACKAGE_ROOT: sdkRoot,
        },
      },
    );
    assert.equal(result.status, 0, result.stderr + result.stdout);
    assert.match(result.stdout, /PRODUCTION_PATH_GATE_CLOSED/);
    assert.match(result.stdout, /PUBLIC_PREFLIGHT_OK/);
    assert.match(result.stdout, /EFFECTIVE_OVERRIDE_REJECTED/);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("index.ts synthetic broker holds lock/active work through stop and lifecycle races", async () => {
  const originalSpawn = childProcess.spawn;
  const processes = [];
  childProcess.spawn = () => {
    const proc = new EventEmitter();
    proc.stdin = new PassThrough();
    proc.stdout = new PassThrough();
    proc.stderr = new PassThrough();
    proc.kill = () => {
      proc.emit("close");
      return true;
    };
    proc.stdin.on("finish", () => proc.emit("close"));
    processes.push(proc);
    queueMicrotask(() =>
      proc.stdout.write('{"type":"status","code":"relay_ready"}\n'),
    );
    return proc;
  };
  syncBuiltinESMExports();
  try {
    for (const lifecycle of [
      "relay-stop",
      "session_before_switch",
      "session_shutdown",
      "model_select",
    ]) {
      const h = harness();
      const root = host(h.native);
      await root.call("relay-preflight");
      await root.call(
        "relay-start",
        JSON.stringify({
          python: resolve("synthetic-python"),
          runtimeProfile: resolve("synthetic-profile"),
        }),
      );
      const proc = processes.at(-1);
      proc.stdout.write(
        JSON.stringify({ type: "generate", ...request }) + "\n",
      );
      await tick();
      assert.equal(h.sent.length, 1);
      proc.stdout.write(
        JSON.stringify({ type: "cancel", stem: "foreign" }) + "\n",
      );
      assert.equal(h.cancels.length, 0);
      const stopped =
        lifecycle === "relay-stop"
          ? root.call("relay-stop")
          : root.hooks.get(lifecycle)();
      await tick();
      assert.equal(proc.stdin.writableEnded, false);
      await root.call("relay-start", "{}");
      assert.equal(root.notices.pop(), "relay_already_running");
      assert.equal(h.cancels.length, 1);
      h.terminal({ status: "cancelled", runId: undefined });
      await stopped;
      assert.equal(proc.stdin.writableEnded, true);
      await root.call("relay-status");
      const status = JSON.parse(root.notices.pop());
      assert.equal(status.running, false);
      assert.equal(status.busy, false);
    }
  } finally {
    childProcess.spawn = originalSpawn;
    syncBuiltinESMExports();
  }
});

test("index.ts absent native terminal stays fail-closed and blocks a replacement broker", async (t) => {
  const originalSpawn = childProcess.spawn;
  let proc;
  childProcess.spawn = () => {
    proc = new EventEmitter();
    proc.stdin = new PassThrough();
    proc.stdout = new PassThrough();
    proc.stderr = new PassThrough();
    proc.kill = () => {
      proc.emit("close");
      return true;
    };
    proc.stdin.on("finish", () => proc.emit("close"));
    queueMicrotask(() =>
      proc.stdout.write('{"type":"status","code":"relay_ready"}\n'),
    );
    return proc;
  };
  syncBuiltinESMExports();
  try {
    const h = harness();
    const root = host(h.native);
    await root.call("relay-preflight");
    await root.call(
      "relay-start",
      JSON.stringify({
        python: resolve("synthetic-python"),
        runtimeProfile: resolve("synthetic-profile"),
      }),
    );
    proc.stdout.write(JSON.stringify({ type: "generate", ...request }) + "\n");
    await tick();
    t.mock.timers.enable({ apis: ["setTimeout"] });
    const stop = root.call("relay-stop");
    t.mock.timers.tick(5001);
    await stop;
    assert.equal(root.notices.pop(), "relay_cancel_unsettled");
    assert.equal(proc.stdin.writableEnded, false);
    await root.call("relay-start", "{}");
    assert.equal(root.notices.pop(), "relay_already_running");
    h.terminal({ status: "cancelled" });
    await tick();
    await root.call("relay-stop");
    assert.equal(proc.stdin.writableEnded, true);
  } finally {
    t.mock.timers.reset();
    childProcess.spawn = originalSpawn;
    syncBuiltinESMExports();
  }
});

test("index.ts real path-policy diagnostic cannot create readiness or admit a broker", async () => {
  const value = contract();
  value.contract.diagnostics.push({
    code: "host_required",
    severity: "host-required",
    message:
      "No sessionRoot/sessionDir was supplied; exact child session paths require the Pi host session-root policy.",
  });
  const h = host(harness(async () => value).native);
  await h.call("relay-preflight");
  assert.equal(h.notices.pop(), "relay_host_validation_pending");
  await h.call("relay-status");
  assert.equal(JSON.parse(h.notices.pop()).preflightVerified, false);
  await h.call("relay-start", "{}");
  assert.equal(h.notices.pop(), "relay_host_validation_pending");
});

// Execute only broker.serve's IPC projection against a synthetic broker object.
// No Broker construction, filesystem spool, reader thread, claim store, or provider.
function minimumBrokerPacket() {
  const script = `
import importlib.util, json, time
from types import SimpleNamespace
spec = importlib.util.spec_from_file_location('relay_broker_fixture', ${JSON.stringify(join(CWD, "broker.py"))})
broker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(broker)
packet = {'_stem':'a'*32+'.1.'+'b'*64, 'job_id':'d'*32, 'prompt':'SYNTHETIC ONLY',
          'max_output_bytes':4096, '_deadline':time.monotonic()+60}
class ProjectionCaptured(Exception): pass
def emit(value):
    if value['type'] == 'generate':
        print(json.dumps(value))
        raise ProjectionCaptured()
broker.emit = emit
broker.threading.Thread = lambda **kwargs: SimpleNamespace(start=lambda:None)
noop = lambda:None
fake = SimpleNamespace(authority=noop, pins=SimpleNamespace(check=noop), lock=SimpleNamespace(check=noop),
    requests=SimpleNamespace(iterdir=lambda:[SimpleNamespace(name='synthetic.packet.json')]),
    packet=lambda path:packet, claim=lambda packet:True)
try: broker.serve(fake)
except ProjectionCaptured: pass
`;
  const result = childProcess.spawnSync("python", ["-I", "-B", "-c", script], {
    encoding: "utf8",
    timeout: 10000,
  });
  assert.equal(result.status, 0, result.stderr);
  const value = JSON.parse(result.stdout);
  assert.equal(value.maxOutputBytes, 3584);
  assert.notEqual(value.stem.split(".")[0], value.jobId);
  return value;
}

for (const [finding, packet] of [
  ["NATIVE-ATTEMPT-BINDING-01", { ...request, jobId: "d".repeat(32) }],
  // broker.py reserves 512 bytes from the 4096-byte packet envelope floor.
  ["NATIVE-OUTPUT-FLOOR-01", { ...request, maxOutputBytes: 4096 - 512 }],
]) {
  test(`${finding}: valid broker IPC reaches delegation`, async () => {
    const originalSpawn = childProcess.spawn;
    let proc;
    const dispatched = [];
    const ipc =
      finding === "NATIVE-OUTPUT-FLOOR-01" ? minimumBrokerPacket() : packet;
    childProcess.spawn = () => {
      proc = new EventEmitter();
      proc.stdin = new PassThrough();
      proc.stdout = new PassThrough();
      proc.stderr = new PassThrough();
      proc.stdin.on("finish", () => proc.emit("close"));
      proc.kill = () => {
        proc.emit("close");
        return true;
      };
      queueMicrotask(() =>
        proc.stdout.write(
          JSON.stringify({ type: "status", code: "relay_ready" }) +
            String.fromCharCode(10),
        ),
      );
      return proc;
    };
    syncBuiltinESMExports();
    const root = host({
      readiness: async () => ({ identity }),
      generate: async (_ctx, value) => {
        dispatched.push(value);
        return { stem: value.stem, output: { job_id: value.jobId } };
      },
    });
    try {
      await root.call("relay-preflight");
      await root.call(
        "relay-start",
        JSON.stringify({
          python: resolve("synthetic-python"),
          runtimeProfile: resolve("synthetic-profile"),
        }),
      );
      proc.stdout.write(
        JSON.stringify({ type: "generate", ...ipc }) + String.fromCharCode(10),
      );
      await tick();
      assert.equal(dispatched.length, 1, finding);
      assert.equal(dispatched[0].jobId, ipc.jobId);
      assert.equal(dispatched[0].maxOutputBytes, ipc.maxOutputBytes);
    } finally {
      await root.call("relay-stop");
      childProcess.spawn = originalSpawn;
      syncBuiltinESMExports();
    }
  });
}

test("NATIVE-OUTPUT-FLOOR-01: model accepts net3584 and enforces exact result limit", async () => {
  const net = 4096 - 512;
  for (const excess of [0, 1]) {
    const h = harness();
    const pending = h.start({ ...request, maxOutputBytes: net });
    await tick();
    assert.equal(h.sent.length, 1, "3584-byte net allowance must dispatch");
    const base = JSON.stringify({ job_id: request.jobId, text: "" });
    const text = JSON.stringify({
      job_id: request.jobId,
      text: "x".repeat(net - Buffer.byteLength(base) + excess),
    });
    assert.equal(Buffer.byteLength(text), net + excess);
    h.terminal({ result: { kind: "text", text } });
    const result = await pending;
    if (excess) assert.equal(result.failed, "relay_output_oversize");
    else assert.equal(result.output.job_id, request.jobId);
  }
  assert.equal(
    (await harness().start({ ...request, maxOutputBytes: net - 1 })).failed,
    "relay_output_limit",
  );
});

test("NATIVE-MODEL-SNAPSHOT-01: copies all public metadata without provider headers", async () => {
  const metadata = {
    provider: "synthetic",
    id: "model",
    api: "synthetic-api",
    reasoning: false,
    contextWindow: 400000,
    maxTokens: 10000,
    input: ["text"],
    thinkingLevelMap: { off: "off" },
    cost: { input: 1, output: 2 },
    headers: { Authorization: "SYNTHETIC_HEADER" },
  };
  let captured;
  const relay = new NativeRelay(bus(), async (value) => {
    captured = value;
    return contract();
  });
  await relay.readiness({
    model: metadata,
    modelRegistry: {
      getAvailable: () => [metadata, { ...metadata, id: "backup" }],
    },
  });
  assert.equal(captured.availableModels.length, 2);
  assert.equal(captured.availableModels[1].fullId, "synthetic/backup");
  assert.equal(captured.availableModels[0].headers, undefined);
  assert.deepEqual(captured.parentModel, {
    provider: "synthetic",
    id: "model",
  });
  metadata.input.push("image");
  metadata.cost.input = 3;
  assert.deepEqual(captured.availableModels[0].input, ["text"]);
  assert.equal(captured.availableModels[0].cost.input, 1);
});
