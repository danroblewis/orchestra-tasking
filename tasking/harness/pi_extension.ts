/**
 * Orchestra Pi extension: registers the tools an agent uses to talk to Orchestra.
 *
 * The tool definitions are Orchestra's (orchestra/harness/tools.py); this file fetches them
 * once at load with `python -m tasking tools --json`, filtered to the groups in
 * ORCHESTRA_TOOLS, and registers each one. Every call shells out to
 * `python -m tasking api <command>` against the project named by ORCHESTRA_REPO, so it
 * works whether or not `tasking run` is up. See SPEC.md section 5.
 */

import { execFileSync } from "node:child_process";
import { Type } from "@earendil-works/pi-ai";
import { defineTool, type ExtensionAPI } from "@earendil-works/pi-coding-agent";

const ROLE = process.env.ORCHESTRA_ROLE ?? "";
const REPO = process.env.ORCHESTRA_REPO ?? "";
const TICKET = process.env.ORCHESTRA_TICKET ?? "";
const PYTHON = process.env.ORCHESTRA_PYTHON ?? "python3";
const ENV = { ...process.env, ORCHESTRA_REPO: REPO, ORCHESTRA_ROLE: ROLE, ORCHESTRA_TICKET: TICKET };

function api(command: string, args: Record<string, unknown>): string {
  try {
    const out = execFileSync(PYTHON, ["-m", "tasking", "api", command, "--args", JSON.stringify(args)], {
      env: ENV, cwd: REPO || process.cwd(), encoding: "utf8", maxBuffer: 8 * 1024 * 1024, timeout: 60_000,
    });
    return out.trim();
  } catch (err: any) {
    const stderr = err?.stderr?.toString?.() ?? "";
    const stdout = err?.stdout?.toString?.() ?? "";
    return JSON.stringify({ ok: false, error: (stdout || stderr || String(err)).trim().slice(-2000) });
  }
}

interface ToolSpec { name: string; description: string; inputSchema: Record<string, unknown>; command: string }

function loadTools(): ToolSpec[] {
  const out = execFileSync(PYTHON, ["-m", "tasking", "tools", "--json"], {
    env: ENV, cwd: REPO || process.cwd(), encoding: "utf8", timeout: 60_000,
  });
  return JSON.parse(out) as ToolSpec[];
}

export default function (pi: ExtensionAPI) {
  for (const t of loadTools()) {
    pi.registerTool(
      defineTool({
        name: t.name,
        label: t.name,
        description: t.description,
        parameters: Type.Unsafe<Record<string, unknown>>(t.inputSchema),
        async execute(_id, p) {
          const text = api(t.command, (p ?? {}) as Record<string, unknown>);
          return { content: [{ type: "text", text }], details: undefined };
        },
      }),
    );
  }
}
