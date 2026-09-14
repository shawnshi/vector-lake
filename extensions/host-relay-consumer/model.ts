import { randomUUID } from "node:crypto";
import { createRequire } from "node:module";
import { homedir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import type {
  ExtensionAPI,
  ExtensionContext,
} from "@earendil-works/pi-coding-agent";

export const MAX_INPUT_BYTES = 196608;
export const MAX_OUTPUT_BYTES = 8 * 1024 * 1024;
// broker.py admits a 4096-byte envelope, then reserves 512 bytes before IPC.
export const MIN_NET_OUTPUT_BYTES = 4096 - 512;
// Context admission reserve, NOT a provider generation cap or tokenizer proof.
export const CONTEXT_RESERVE = 16384 + 1024;
export const AGENT = "host-relay-json";
export const CWD = dirname(fileURLToPath(import.meta.url));
export const AGENT_FILE = join(CWD, ".pi", "agents", `${AGENT}.md`);
export const EVENTS = {
  request: "prompt-template:subagent:request",
  started: "prompt-template:subagent:started",
  update: "prompt-template:subagent:update",
  response: "prompt-template:subagent:response",
  cancel: "prompt-template:subagent:cancel",
} as const;
export type Request = {
  stem: string;
  jobId: string;
  prompt: string;
  maxOutputBytes: number;
  timeoutMs: number;
};
type Context = Pick<ExtensionContext, "model" | "modelRegistry">;
type Bus = ExtensionAPI["events"];
// Public preflight DTO is checked at the boundary, never trusted by assertion/cast.
type Preflight = (input: Record<string, unknown>) => Promise<any>;
const ERRORS = new Set([
  "relay_host_forbidden",
  "relay_options_invalid",
  "relay_already_running",
  "relay_broker_not_ready",
  "relay_model_unavailable",
  "relay_context_limit_invalid",
  "relay_input_limit",
  "relay_output_limit",
  "relay_timeout_invalid",
  "relay_native_unavailable",
  "relay_host_validation_pending",
  "relay_preflight_rejected",
  "relay_agent_mismatch",
  "relay_model_mismatch",
  "relay_tools_forbidden",
  "relay_extensions_forbidden",
  "relay_context_forbidden",
  "relay_output_oversize",
  "relay_output_json",
  "relay_output_binding",
  "relay_native_failed",
  "relay_native_contract_mismatch",
  "relay_native_result_invalid",
  "relay_cancelled",
  "relay_timeout",
  "relay_native_dispatch_error",
  "relay_preflight_required",
]);
export class RelayError extends Error {}
function guard(ok: unknown, code: string): asserts ok {
  if (!ok) throw new RelayError(code);
}
export function errorCode(error: unknown): string {
  try {
    const value =
      error instanceof Error
        ? Object.getOwnPropertyDescriptor(error, "message")?.value
        : undefined;
    if (typeof value === "string" && ERRORS.has(value)) return value;
  } catch {
    /* Exception objects, including proxies, are opaque. */
  }
  return "relay_native_failed";
}
export function modelAllowed(ctx: Context) {
  const model = ctx.model;
  guard(
    model &&
      typeof model.provider === "string" &&
      model.provider.length > 0 &&
      typeof model.id === "string" &&
      model.id.length > 0,
    "relay_model_unavailable",
  );
  guard(
    Number.isFinite(model.contextWindow) &&
      model.contextWindow >= MAX_INPUT_BYTES + CONTEXT_RESERVE,
    "relay_context_limit_invalid",
  );
  return model;
}

/** Resolve the installed package's PUBLIC export. Pi's extension loader handles TS modules.
 * No import of runners, auth, transport, or global capability-ceiling registration. */
export async function loadPreflight(): Promise<Preflight> {
  try {
    const installed = createRequire(
      join(homedir(), ".pi", "agent", "npm", "package.json"),
    );
    const entry = installed.resolve("pi-subagents/preflight");
    const api = await import(entry);
    guard(
      typeof api.resolveSubagentLaunchContract === "function",
      "relay_native_unavailable",
    );
    return api.resolveSubagentLaunchContract;
  } catch {
    throw new RelayError("relay_native_unavailable");
  }
}
const empty = (value: unknown) => Array.isArray(value) && value.length === 0;
const samePath = (a: unknown, b: string) =>
  typeof a === "string" && resolve(a) === resolve(b);
export function verifyContract(result: any, model: string): string {
  guard(
    result?.ok === true &&
      result.contract?.version === 3 &&
      result.contract.protocol?.packageVersion === "0.67.0",
    "relay_preflight_rejected",
  );
  const c = result.contract;
  guard(
    c.agent?.name === AGENT &&
      c.agent.source === "project" &&
      samePath(c.agent.filePath, AGENT_FILE),
    "relay_agent_mismatch",
  );
  guard(
    c.model === `${model}:off` &&
      Array.isArray(c.modelCandidates) &&
      c.modelCandidates.length === 1 &&
      c.modelCandidates[0] === `${model}:off`,
    "relay_model_mismatch",
  );
  guard(
    c.context === "fresh" &&
      c.systemPromptMode === "replace" &&
      c.inheritProjectContext === false &&
      c.inheritGlobalContext === false &&
      c.inheritSkills === false &&
      empty(c.skills?.requested) &&
      empty(c.skills?.resolved) &&
      empty(c.skills?.missing) &&
      c.intercomBridge?.mode === "off" &&
      c.intercomBridge.active === false &&
      samePath(c.roots?.cwd, CWD) &&
      !c.roots.outputPath &&
      !c.roots.artifactsDir,
    "relay_context_forbidden",
  );
  const t = c.tools;
  guard(
    t?.explicitAllowlist === true &&
      t.fanoutAuthorized === false &&
      [
        t.requestedBuiltin,
        t.declaredBuiltin,
        t.effectiveAllowlist,
        t.requiredChildTools,
        t.internalTools,
        t.mcp,
        t.effectiveMcpTools,
      ].every(empty),
    "relay_tools_forbidden",
  );
  // pi-subagents' prompt runtime is intrinsic, not an ambient extension.
  guard(
    t.disableAmbientExtensions === true &&
      empty(t.configuredExtensions) &&
      empty(t.toolExtensionPaths) &&
      Array.isArray(t.runtimeExtensions) &&
      t.runtimeExtensions.length === 1 &&
      t.runtimeExtensions[0]
        .split(String.fromCharCode(92))
        .join("/")
        .endsWith("/pi-subagents/src/runs/shared/subagent-prompt-runtime.ts") &&
      Array.isArray(t.extensionArgs) &&
      t.extensionArgs.length === 1 &&
      t.extensionArgs[0] === t.runtimeExtensions[0],
    "relay_extensions_forbidden",
  );
  guard(
    Array.isArray(c.diagnostics) &&
      !c.diagnostics.some((d: any) => d.severity === "error"),
    "relay_preflight_rejected",
  );
  guard(
    !c.diagnostics.some((d: any) => d.severity === "host-required"),
    "relay_host_validation_pending",
  );
  guard(
    typeof c.launchContractDigest === "string" &&
      /^[a-f0-9]{64}$/.test(c.launchContractDigest),
    "relay_preflight_rejected",
  );
  return c.launchContractDigest;
}
export function parseOutput(text: unknown, request: Request) {
  guard(typeof text === "string", "relay_native_result_invalid");
  guard(
    Buffer.byteLength(text, "utf8") <= request.maxOutputBytes,
    "relay_output_oversize",
  );
  let output: unknown;
  try {
    output = JSON.parse(text);
  } catch {
    throw new RelayError("relay_output_json");
  }
  guard(
    output &&
      typeof output === "object" &&
      !Array.isArray(output) &&
      (output as Record<string, unknown>).job_id === request.jobId,
    "relay_output_binding",
  );
  return { stem: request.stem, output };
}
const TERMINAL = new Set([
  "completed",
  "failed",
  "timed_out",
  "cancelled",
  "interrupted",
  "tool_budget_exhausted",
  "structured_output_failed",
  "acceptance_failed",
  "invalid_request",
  "unavailable_context",
  "duplicate_node",
]);

export class NativeRelay {
  private busy = false;
  private events: Bus;
  private preflight: Preflight;
  constructor(events: Bus, preflight: Preflight) {
    this.events = events;
    this.preflight = preflight;
  }
  async readiness(ctx: Context, task = "SYNTHETIC NO-NETWORK PREFLIGHT") {
    const model = modelAllowed(ctx);
    const identity = `${model.provider}/${model.id}`;
    // Use every currently available model, as native execution does. A one-model
    // snapshot silently filters configured fallbacks out of the public preflight.
    // Copy only public model metadata, never provider headers/auth configuration.
    const availableModels = ctx.modelRegistry
      .getAvailable()
      .map(
        ({
          provider,
          id,
          api,
          reasoning,
          thinkingLevelMap,
          contextWindow,
          maxTokens,
          input,
          cost,
        }) =>
          structuredClone({
            provider,
            id,
            fullId: `${provider}/${id}`,
            api,
            reasoning,
            thinkingLevelMap,
            contextWindow,
            maxTokens,
            input,
            cost,
          }),
      );
    const result = await this.preflight({
      agent: AGENT,
      cwd: CWD,
      task,
      context: "fresh",
      model: identity,
      thinking: "off",
      availableModels,
      parentModel: { provider: model.provider, id: model.id },
      skill: false,
      artifacts: false,
      intercomBridge: { mode: "off" },
    });
    return { identity, digest: verifyContract(result, identity) };
  }
  async generate(ctx: Context, request: Request, controller: AbortController) {
    if (this.busy)
      return { stem: request.stem, failed: "relay_already_running" };
    this.busy = true;
    try {
      guard(
        typeof request.prompt === "string" &&
          Buffer.byteLength(request.prompt, "utf8") <= MAX_INPUT_BYTES,
        "relay_input_limit",
      );
      guard(
        Number.isInteger(request.maxOutputBytes) &&
          request.maxOutputBytes >= MIN_NET_OUTPUT_BYTES &&
          request.maxOutputBytes <= MAX_OUTPUT_BYTES,
        "relay_output_limit",
      );
      guard(
        Number.isInteger(request.timeoutMs) &&
          request.timeoutMs > 0 &&
          request.timeoutMs <= 900000,
        "relay_timeout_invalid",
      );
      guard(!controller.signal.aborted, "relay_cancelled");
      const deadline = Date.now() + request.timeoutMs;
      const { identity, digest } = await this.readiness(ctx, request.prompt);
      guard(!controller.signal.aborted, "relay_cancelled");
      guard(Date.now() < deadline, "relay_timeout");
      guard(
        `${ctx.model?.provider}/${ctx.model?.id}` === identity,
        "relay_model_mismatch",
      );
      return await this.delegate(
        request,
        controller,
        identity,
        digest,
        deadline,
      );
    } catch (error) {
      return { stem: request.stem, failed: errorCode(error) };
    } finally {
      this.busy = false;
    }
  }
  private delegate(
    request: Request,
    controller: AbortController,
    model: string,
    digest: string,
    deadline: number,
  ): Promise<any> {
    const binding = {
      requestId: randomUUID(),
      ownerRunId: randomUUID(),
      nodeId: request.stem,
    };
    return new Promise((resolveResult) => {
      let done = false;
      let dispatched = false;
      let runId: string | undefined;
      let failure: string | undefined;
      const subscriptions: Array<() => void> = [];
      const matches = (e: any) =>
        e &&
        e.requestId === binding.requestId &&
        e.ownerRunId === binding.ownerRunId &&
        e.nodeId === binding.nodeId;
      const cancel = (code: string) => {
        if (done) return;
        failure ??= code;
        if (dispatched) {
          try {
            this.events.emit(EVENTS.cancel, binding);
          } catch {
            /* No terminal proof: keep the attempt busy and fail closed. */
          }
        }
      };
      const aborted = () => cancel("relay_cancelled");
      const timer = setTimeout(
        () => cancel("relay_timeout"),
        Math.max(1, deadline - Date.now()),
      );
      const finish = (result: any) => {
        if (done) return;
        done = true;
        clearTimeout(timer);
        controller.signal.removeEventListener("abort", aborted);
        for (const unsubscribe of subscriptions) unsubscribe();
        resolveResult(result);
      };
      subscriptions.push(
        this.events.on(EVENTS.started, (e: any) => {
          if (matches(e) && failure) cancel(failure);
        }),
      );
      subscriptions.push(
        this.events.on(EVENTS.update, (e: any) => {
          if (!matches(e) || done) return;
          if (e.runId !== undefined) {
            if (typeof e.runId !== "string" || (runId && runId !== e.runId))
              return;
            runId = e.runId;
          }
          if (
            (e.model !== undefined && e.model !== `${model}:off`) ||
            (e.toolCount !== undefined && e.toolCount !== 0) ||
            e.currentTool
          )
            cancel("relay_native_contract_mismatch");
          // Never retain/log recentOutput, arguments, tools, or native errors.
        }),
      );
      subscriptions.push(
        this.events.on(EVENTS.response, (e: any) => {
          if (
            !matches(e) ||
            done ||
            !TERMINAL.has(e.status) ||
            (runId && e.runId !== undefined && e.runId !== runId)
          )
            return;
          try {
            if (failure) throw new RelayError(failure);
            if (e.status === "timed_out") throw new RelayError("relay_timeout");
            if (e.status === "cancelled")
              throw new RelayError("relay_cancelled");
            guard(e.status === "completed", "relay_native_failed");
            guard(
              e.agent === AGENT &&
                e.model === `${model}:off` &&
                e.launchContractDigest === digest &&
                typeof e.runId === "string" &&
                e.usage?.toolCalls === 0,
              "relay_native_contract_mismatch",
            );
            guard(e.result?.kind === "text", "relay_native_result_invalid");
            finish(parseOutput(e.result.text, request));
          } catch (error) {
            finish({ stem: request.stem, failed: errorCode(error) });
          }
        }),
      );
      controller.signal.addEventListener("abort", aborted, { once: true });
      if (controller.signal.aborted) {
        finish({ stem: request.stem, failed: "relay_cancelled" }); // No request emitted.
        return;
      }
      dispatched = true;
      try {
        this.events.emit(EVENTS.request, {
          ...binding,
          agent: AGENT,
          task: request.prompt,
          context: "fresh",
          cwd: CWD,
          model,
          thinking: "off",
          timeoutMs: Math.max(1, deadline - Date.now()),
          toolBudget: { hard: 0, block: "*" },
          skill: false,
          artifacts: false,
          intercomBridge: { mode: "off" },
          result: { kind: "text" },
        });
      } catch {
        cancel("relay_native_dispatch_error");
      } // Ambiguous delivery must remain busy.
    });
  }
}
