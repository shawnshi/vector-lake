import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { homedir } from "node:os";
import { isAbsolute, join } from "node:path";
import { fileURLToPath } from "node:url";
import type {
  ExtensionAPI,
  ExtensionCommandContext,
} from "@earendil-works/pi-coding-agent";
import {
  NativeRelay,
  MIN_NET_OUTPUT_BYTES,
  loadPreflight,
  errorCode,
  type Request,
} from "./model.ts";

const MAX_IPC = 8 * 1024 * 1024;
const BROKER = fileURLToPath(new URL("./broker.py", import.meta.url));
const LOCK = join(homedir(), ".pi", "agent", ".host-relay-consumer.lock");
// Broker codes are fixed protocol diagnostics, never arbitrary strings.
const BROKER_CODES = new Set([
  "relay_already_answered",
  "relay_authority_changed",
  "relay_broker_io_or_schema_error",
  "relay_circuit_open",
  "relay_consent_required",
  "relay_database_changed",
  "relay_directory_changed",
  "relay_failure_code",
  "relay_file_changed",
  "relay_file_hardlink",
  "relay_file_oversize",
  "relay_generation_timeout",
  "relay_input_oversize",
  "relay_ipc_oversize",
  "relay_json_object_required",
  "relay_lease_expired",
  "relay_lease_invalid",
  "relay_lock_changed",
  "relay_meta_outside_memory",
  "relay_output_binding",
  "relay_output_oversize",
  "relay_packet_changed",
  "relay_packet_expired",
  "relay_packet_filename",
  "relay_packet_generation",
  "relay_packet_identity",
  "relay_packet_limits",
  "relay_packet_nonce",
  "relay_packet_output_limit",
  "relay_packet_prompt",
  "relay_packet_protocol",
  "relay_path_kind",
  "relay_path_link",
  "relay_path_relative",
  "relay_path_reparse",
  "relay_profile_required",
  "relay_profile_schema",
  "relay_protocol_invalid",
  "relay_protocol_version",
  "relay_published",
  "relay_ready",
  "relay_result_binding",
  "relay_scan_limit",
  "relay_short_write",
  "relay_spool_outside_meta",
  "relay_temporary_changed",
  "relay_time_invalid",
  "relay_time_timezone_required",
]);
function eligible(ctx: ExtensionCommandContext) {
  if (
    process.env.PI_SUBAGENT_CHILD === "1" ||
    !["tui", "rpc"].includes(ctx.mode)
  )
    throw new Error("relay_host_forbidden");
}
function options(text: string) {
  const value = JSON.parse(text);
  if (
    !value ||
    typeof value !== "object" ||
    Array.isArray(value) ||
    Object.keys(value).some(
      (key) => !["python", "runtimeProfile", "profile"].includes(key),
    ) ||
    typeof value.python !== "string" ||
    !isAbsolute(value.python) ||
    typeof value.runtimeProfile !== "string" ||
    !isAbsolute(value.runtimeProfile) ||
    (value.profile !== undefined &&
      (typeof value.profile !== "string" ||
        !/^[\w-]{1,64}$/.test(value.profile)))
  )
    throw new Error("relay_options_invalid");
  return value as { python: string; runtimeProfile: string; profile?: string };
}
function safeEnvironment() {
  const env: NodeJS.ProcessEnv = {
    PYTHONUTF8: "1",
    PYTHONDONTWRITEBYTECODE: "1",
  };
  for (const key of [
    "SystemRoot",
    "WINDIR",
    "USERPROFILE",
    "HOME",
    "TEMP",
    "TMP",
    "LOCALAPPDATA",
  ])
    if (process.env[key]) env[key] = process.env[key];
  return env;
}
async function settled(promise: Promise<unknown>, ms: number) {
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    return await Promise.race([
      promise.then(
        () => true,
        () => true,
      ),
      new Promise<boolean>((resolve) => {
        timer = setTimeout(() => resolve(false), ms);
      }),
    ]);
  } finally {
    if (timer) clearTimeout(timer);
  }
}

export default function relayConsumer(
  pi: ExtensionAPI,
  createNative: () => Promise<NativeRelay> = async () =>
    new NativeRelay(pi.events, await loadPreflight()),
) {
  // Registration only: no timers, process, model work, or startup from the factory.
  let child: ChildProcessWithoutNullStreams | undefined;
  let closed: Promise<void> | undefined;
  let active: Promise<unknown> | undefined;
  let controller: AbortController | undefined;
  let activeStem: string | undefined;
  let stopping = false;
  let proof: string | undefined;
  let native: NativeRelay | undefined;
  let commandBusy = false;
  let epoch = 0;
  let code = "relay_off";
  let published = 0;
  let lastFailure: string | undefined;

  async function stop() {
    stopping = true;
    epoch++;
    proof = undefined;
    controller?.abort();
    // Only a correlated native terminal settles cancellation; it is not remote HTTP stop proof.
    if (active && !(await settled(active, 5000))) {
      code = "relay_cancel_unsettled";
      return;
    }
    child?.stdin.end();
    if (closed && !(await settled(closed, 3000))) {
      child?.kill();
      if (!(await settled(closed, 1000))) {
        code = "relay_broker_stop_unsettled";
        return;
      }
    }
    code = "relay_off";
    stopping = false;
  }

  async function launch(
    args: ReturnType<typeof options>,
    ctx: ExtensionCommandContext,
  ) {
    if (child || active) throw new Error("relay_already_running");
    stopping = false;
    const proc = spawn(
      args.python,
      [
        "-I",
        "-u",
        BROKER,
        "--lock",
        LOCK,
        "--runtime-profile",
        args.runtimeProfile,
        "--profile",
        args.profile ?? "default",
      ],
      {
        shell: false,
        windowsHide: true,
        env: safeEnvironment(),
        stdio: "pipe",
      },
    );
    child = proc;
    let pending = Buffer.alloc(0);
    let ready: (() => void) | undefined;
    const readyPromise = new Promise<void>((resolve) => {
      ready = resolve;
    });
    closed = new Promise((resolve) => {
      proc.once("close", () => {
        controller?.abort();
        if (child === proc) child = undefined;
        if (code === "relay_ready") code = "relay_broker_closed";
        resolve();
      });
    });
    proc.on("error", () => {
      code = "relay_spawn_error";
      controller?.abort();
    });
    proc.stdin.on("error", () => {
      code = "relay_ipc_error";
      controller?.abort();
    });
    proc.stderr.resume(); // Discard; never persist interpreter/provider exception text.
    proc.stdout.on("data", (chunk: Buffer) => {
      if (pending.length + chunk.length > MAX_IPC) {
        code = "relay_ipc_oversize";
        void stop();
        return;
      }
      pending = Buffer.concat([pending, chunk]);
      let newline: number;
      while ((newline = pending.indexOf(10)) >= 0) {
        const line = pending.subarray(0, newline);
        pending = pending.subarray(newline + 1);
        try {
          const message = JSON.parse(line.toString("utf8"));
          if (
            message.type === "status" &&
            typeof message.code === "string" &&
            BROKER_CODES.has(message.code)
          ) {
            code = message.code;
            if (code === "relay_ready") ready?.();
            if (code === "relay_published") published++;
          } else if (message.type === "cancel") {
            if (message.stem === activeStem) controller?.abort();
          } else if (message.type === "generate" && !stopping && !active) {
            if (
              !native ||
              `${ctx.model?.provider}/${ctx.model?.id}` !== proof ||
              typeof message.prompt !== "string" ||
              typeof message.stem !== "string" ||
              !/^[0-9a-f]{32}\.[1-9][0-9]*\.[0-9a-f]{64}$/.test(message.stem) ||
              typeof message.jobId !== "string" ||
              !/^[0-9a-f]{32}$/.test(message.jobId) ||
              !Number.isInteger(message.maxOutputBytes) ||
              message.maxOutputBytes < MIN_NET_OUTPUT_BYTES ||
              message.maxOutputBytes > 1024 * 1024
            )
              throw new Error("invalid");
            controller = new AbortController();
            activeStem = message.stem;
            const request = message as Request;
            active = native
              .generate(ctx, request, controller)
              .then((result) => {
                if (!stopping && child === proc) {
                  const text = JSON.stringify(result) + "\n";
                  if (Buffer.byteLength(text) > MAX_IPC)
                    throw new Error("oversize");
                  proc.stdin.write(text);
                }
              })
              .catch(() => {
                code = "relay_ipc_error";
                controller?.abort();
                proc.stdin.end();
              })
              .finally(() => {
                active = undefined;
                controller = undefined;
                activeStem = undefined;
              });
          } else throw new Error("invalid");
        } catch {
          code = "relay_ipc_invalid";
          void stop();
          return;
        }
      }
    });
    if (
      !(await settled(readyPromise, 5000)) ||
      child !== proc ||
      code !== "relay_ready"
    ) {
      await stop();
      throw new Error("relay_broker_not_ready");
    }
  }

  pi.registerCommand("relay-preflight", {
    description:
      "No-network native configuration check; does not prove authentication or host acceptance",
    handler: async (_text, ctx) => {
      if (child || active || commandBusy || stopping) {
        ctx.ui.notify("relay_already_running", "error");
        return;
      }
      commandBusy = true;
      const currentEpoch = epoch;
      try {
        eligible(ctx);
        native ??= await createNative();
        const ready = await native.readiness(ctx);
        if (currentEpoch !== epoch) throw new Error("relay_cancelled");
        proof = ready.identity;
        lastFailure = undefined;
        ctx.ui.notify("relay_preflight_ok_no_network", "info");
      } catch (error) {
        proof = undefined;
        lastFailure = errorCode(error);
        ctx.ui.notify(lastFailure, "error");
      } finally {
        commandBusy = false;
      }
    },
  });
  pi.registerCommand("relay-start", {
    description:
      "Explicitly start reviewed relay; JSON {python:absolutePath,runtimeProfile:absolutePath,profile?}",
    handler: async (text, ctx) => {
      if (child || active || commandBusy || stopping) {
        ctx.ui.notify("relay_already_running", "error");
        return;
      }
      commandBusy = true;
      const currentEpoch = epoch;
      try {
        eligible(ctx);
        if (
          !native ||
          !proof ||
          proof !== `${ctx.model?.provider}/${ctx.model?.id}`
        )
          throw new Error(
            lastFailure === "relay_host_validation_pending"
              ? lastFailure
              : "relay_preflight_required",
          );
        const args = options(text);
        await native.readiness(ctx); // Check effective config again before acquiring a broker.
        if (currentEpoch !== epoch) throw new Error("relay_cancelled");
        await launch(args, ctx);
        ctx.ui.notify("relay_ready", "info");
      } catch (error) {
        lastFailure = errorCode(error);
        ctx.ui.notify(lastFailure, "error");
      } finally {
        commandBusy = false;
      }
    },
  });
  pi.registerCommand("relay-stop", {
    description: "Cancel native work; keep lock until native terminal",
    handler: async (_text, ctx) => {
      await stop();
      ctx.ui.notify(code, "info");
    },
  });
  pi.registerCommand("relay-status", {
    description: "Content-free controlled consumer status",
    handler: async (_text, ctx) => {
      ctx.ui.notify(
        JSON.stringify({
          code,
          running: !!child,
          busy: !!active || commandBusy,
          published,
          preflightVerified: !!proof,
          nativeAcceptance: "not_verified",
          ...(lastFailure ? { lastFailure } : {}),
        }),
        "info",
      );
    },
  });
  pi.on("session_shutdown", stop);
  pi.on("session_before_switch", stop);
  pi.on("model_select", stop);
}
