/**
 * Studio extension for pi: what the assistant needs that a coding agent does not have.
 *
 * - Four Altinn tools, which call the assistant's Python domain library through studio_tool.py.
 * - A path guard: file tools stay in the app, and may read the skills and the attachments. They may not
 *   write the generated model files. The guard compares real paths, so a symbolic link in the app cannot
 *   lead outside it. Bash is not confined here; the sandbox around pi is the boundary.
 * - A settle gate: the run cannot end while verify_changes fails on the changed files. It does
 *   what commit_session_branch's refusal does today, without a commit tool.
 *
 * Configuration comes from environment variables that the runner sets:
 *   STUDIO_PYTHON, STUDIO_TOOL      the interpreter and studio_tool.py
 *   STUDIO_START_COMMIT             the commit the turn started from
 *   STUDIO_SKILLS_DIR               read-only for the file tools
 *   STUDIO_EVENTS                   optional JSONL file for the gate's decisions
 */

import { execFile, execFileSync } from 'node:child_process';
import { appendFileSync, lstatSync, realpathSync } from 'node:fs';
import path from 'node:path';
import { Type } from '@earendil-works/pi-ai';
import type { ExtensionAPI } from '@earendil-works/pi-coding-agent';

const PYTHON = process.env.STUDIO_PYTHON ?? 'python3';
const TOOL = process.env.STUDIO_TOOL ?? path.join(__dirname, '..', 'studio_tool.py');
const BASE = process.env.STUDIO_START_COMMIT ?? 'HEAD';
const SKILLS_DIR = process.env.STUDIO_SKILLS_DIR;
const ATTACHMENTS_DIR = process.env.STUDIO_ATTACHMENTS_DIR;
const EVENTS = process.env.STUDIO_EVENTS;
const MAX_GATE_ROUNDS = 2;

const DESCRIPTIONS: Record<string, string> = {
  verify_changes:
    "Validate the app files you changed against Altinn's schemas and rules: layout files, Settings.json, text " +
    'resources, JSON syntax, text keys and page navigation. Call it after your last change, and fix every error it ' +
    'reports. The turn cannot end while it fails. Takes no input.',
  altinn_layout_props:
    "List the properties that a layout component type allows, from the app's own layout schema. Call it before you " +
    'add a component or a property.',
  altinn_datamodel_sync:
    'Generate the C# class and the XSD of a data model from App/models/<name>.schema.json, with the same code as ' +
    "'Generate model' in Studio. Call it after you change a schema, then read the generated files again before you " +
    'change them.',
  altinn_policy_subjects:
    'Look up access packages and roles in Altinn Access Management, to write the subjects of a policy rule.',
};

type ToolOutput = { is_error: boolean; content: string };

function runStudioTool(
  name: string,
  cwd: string,
  args: unknown,
  signal?: AbortSignal,
): Promise<ToolOutput> {
  return new Promise((resolve) => {
    execFile(
      PYTHON,
      [TOOL, name, '--repo', cwd, '--base', BASE, '--args', JSON.stringify(args ?? {})],
      { signal, maxBuffer: 16 * 1024 * 1024, timeout: 120_000 },
      (error, stdout, stderr) => {
        if (error && !stdout) {
          resolve({ is_error: true, content: `${name} could not run: ${stderr || error.message}` });
          return;
        }
        try {
          resolve(JSON.parse(stdout) as ToolOutput);
        } catch {
          resolve({
            is_error: true,
            content: `${name} gave output that is not JSON: ${stdout.slice(0, 2000)}`,
          });
        }
      },
    );
  });
}

function record(event: Record<string, unknown>): void {
  if (EVENTS)
    appendFileSync(EVENTS, `${JSON.stringify({ at: new Date().toISOString(), ...event })}\n`);
}

function inside(child: string, parent: string): boolean {
  const relative = path.relative(parent, child);
  return relative === '' || (!relative.startsWith('..') && !path.isAbsolute(relative));
}

// The path after all symbolic links. An app repository can contain links, and pi's tools follow them, so the guard
// must compare where a path really leads. A path that does not exist yet, such as a new file, takes the real path of
// its nearest part that exists. A link that leads nowhere throws, and the guard then blocks the call, since a write
// through it would create its target.
function realPath(target: string): string {
  let existing = target;
  const rest: string[] = [];
  for (;;) {
    try {
      lstatSync(existing);
      break;
    } catch {
      const parent = path.dirname(existing);
      if (parent === existing) break;
      rest.unshift(path.basename(existing));
      existing = parent;
    }
  }
  return path.join(realpathSync(existing), ...rest);
}

const FILE_TOOLS = new Set(['read', 'write', 'edit', 'grep', 'find', 'ls']);
const READ_ONLY_TOOLS = new Set(['read', 'grep', 'find', 'ls']);
// The C# class and the XSD of a data model. Studio generates them, so a hand edit makes them disagree with the schema.
const GENERATED = /^App\/models\/[^/]+\.(cs|xsd)$/;
const BLOCKED_COMMANDS: Array<[RegExp, string]> = [
  [
    /\bgit\s+(commit|push|checkout|switch|reset|rebase|merge)\b/,
    'Studio commits and pushes your changes after the turn.',
  ],
  [
    /\bdotnet\b/,
    'Building and running the app is not available. Check your changes with verify_changes.',
  ],
];

export default function studio(pi: ExtensionAPI) {
  const schemas = JSON.parse(
    execFileSync(PYTHON, [TOOL, 'describe'], { encoding: 'utf8' }),
  ) as Record<string, Record<string, unknown>>;

  for (const [name, schema] of Object.entries(schemas)) {
    pi.registerTool({
      name,
      label: name,
      description: DESCRIPTIONS[name] ?? name,
      parameters: Type.Unsafe(schema),
      async execute(_toolCallId, params, signal, _onUpdate, ctx) {
        const result = await runStudioTool(name, ctx.cwd, params, signal);
        return {
          content: [{ type: 'text', text: result.content }],
          details: undefined,
          isError: result.is_error,
        };
      },
    });
  }

  pi.on('tool_call', async (event, ctx) => {
    if (event.toolName === 'bash') {
      const command = String((event.input as { command?: unknown }).command ?? '');
      for (const [pattern, reason] of BLOCKED_COMMANDS) {
        if (pattern.test(command)) {
          record({ type: 'blocked', tool: 'bash', command });
          return { block: true, reason };
        }
      }
      return undefined;
    }
    if (!FILE_TOOLS.has(event.toolName)) return undefined;

    const target = path.resolve(ctx.cwd, String((event.input as { path?: unknown }).path ?? '.'));
    let real: string;
    let app: string;
    try {
      real = realPath(target);
      app = realpathSync(ctx.cwd);
    } catch {
      record({ type: 'blocked', tool: event.toolName, path: target, reason: 'unresolvable' });
      return {
        block: true,
        reason: `${target} is a link that leads nowhere. Work only on files under ${ctx.cwd}.`,
      };
    }
    if (!READ_ONLY_TOOLS.has(event.toolName) && GENERATED.test(path.relative(app, real))) {
      record({ type: 'blocked', tool: event.toolName, path: target });
      return {
        block: true,
        reason:
          'altinn_datamodel_sync generates this file from the JSON schema. Change the schema, then call it.',
      };
    }
    if (inside(real, app)) return undefined;
    for (const readable of [SKILLS_DIR, ATTACHMENTS_DIR]) {
      if (readable && READ_ONLY_TOOLS.has(event.toolName) && inside(real, realpathSync(readable)))
        return undefined;
    }
    record({ type: 'blocked', tool: event.toolName, path: target, real });
    return {
      block: true,
      reason: `${target} is outside the app. Work only on files under ${ctx.cwd}.`,
    };
  });

  let gateRounds = 0;
  pi.on('agent_before_settle', async (event, ctx) => {
    if (event.outcome !== 'completed') return undefined;
    const result = await runStudioTool('verify_changes', ctx.cwd, {});
    const nothingChanged = result.is_error && result.content.startsWith('No changed files');
    const passed = !result.is_error || nothingChanged;
    record({ type: 'gate', round: gateRounds, passed, nothing_changed: nothingChanged });
    if (passed || gateRounds >= MAX_GATE_ROUNDS) return undefined;

    gateRounds += 1;
    return {
      entries: [
        {
          type: 'custom_message',
          customType: 'studio-verify-gate',
          display: false,
          content:
            'verify_changes fails on the files you changed, so Studio cannot save them. Fix these errors, run ' +
            `verify_changes again, and then finish with your summary.\n\n${result.content}`,
        },
      ],
      continue: true,
    };
  });
}
