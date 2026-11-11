/* The browser loads these files directly — there is no build step, so nothing
 * stands between a typo and the page except the browser's own parser. That is how
 * `panel.js` shipped with a stray `,` after a `return`: every other test imported
 * `catalog.js` / `client.js` / `wire.js`, and nobody imported the one file the whole
 * drawer hangs off.
 *
 * Three guards, all cheap:
 *   1. every shipped module must parse as an ES module;
 *   2. every relative `import … from "…"` must point at a file that exists —
 *      `node --check` parses only, it never resolves specifiers, so a typo'd path in
 *      `app.js` would otherwise pass and break the page;
 *   3. `createAgentPanel()` must be constructible on a page with no document.
 *
 * `panel.js` is imported lazily on purpose: with a static import, a syntax error in
 * it aborts this whole module at load time and the guard that names the offending
 * file never gets to run.
 *
 * `node --check` is only trustworthy on `.mjs`: given a `.js` file it parses as
 * CommonJS and silently skips module syntax, so a broken `export` module passes.
 * Hence the copy.
 */

import test from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const WEB = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "src", "oacp", "web");

/** What the page actually loads. Named, so a missing one fails by name. */
const SHIPPED = [
  "app.js",
  "md.js",
  "agent/panel.js",
  "agent/client.js",
  "agent/wire.js",
  "agent/catalog.js",
  "agent/tablock.js",
];

function modulesIn(dir) {
  const found = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) found.push(...modulesIn(path));
    else if (entry.name.endsWith(".js")) found.push(path);
  }
  return found.sort();
}

test("every browser module parses as an ES module", () => {
  const files = modulesIn(WEB);
  const shipped = files.map((file) => relative(WEB, file).replace(/[\\/]/g, "/"));
  for (const expected of SHIPPED) {
    assert.ok(shipped.includes(expected), `the page loads ${expected}, but it is not there`);
  }
  const scratch = mkdtempSync(join(tmpdir(), "oacp-modules-"));
  try {
    for (const file of files) {
      // Mirror the tree instead of flattening it: any flattening scheme can collide
      // (`agent/panel.js` vs a real `agent__panel.js`), and a collision silently
      // leaves one module unchecked.
      const copy = join(scratch, relative(WEB, file).replace(/\.js$/, ".mjs"));
      mkdirSync(dirname(copy), { recursive: true });
      writeFileSync(copy, readFileSync(file));
      const result = spawnSync(process.execPath, ["--check", copy], { encoding: "utf8" });
      assert.equal(
        result.error,
        undefined,
        `could not run node --check for ${file}: ${result.error && result.error.message}`,
      );
      assert.equal(result.status, 0, `${file} does not parse:\n${result.stderr || result.stdout}`);
    }
  } finally {
    rmSync(scratch, { recursive: true, force: true });
  }
});

test("every relative import points at a file that exists", () => {
  const missing = [];
  for (const file of modulesIn(WEB)) {
    const source = readFileSync(file, "utf8");
    const specifiers = [
      ...source.matchAll(/\bfrom\s+["']([^"']+)["']/g),
      ...source.matchAll(/^\s*import\s+["']([^"']+)["']/gm),
    ].map((match) => match[1]);
    for (const specifier of specifiers) {
      if (!specifier.startsWith(".")) continue; // bare specifiers are not this page's business
      if (!existsSync(resolve(dirname(file), specifier))) {
        missing.push(`${relative(WEB, file)} → ${specifier}`);
      }
    }
  }
  assert.deepEqual(missing, [], `these imports point at nothing:\n${missing.join("\n")}`);
});

test("the panel is constructible on a page with no document", async () => {
  assert.equal(
    typeof globalThis.document,
    "undefined",
    "this guard is only meaningful on a page with no document",
  );
  const { createAgentPanel } = await import(pathToFileURL(join(WEB, "agent", "panel.js")).href);
  // `boot()` must consult `document` before it touches the host. A host that only
  // throws would hide a wrong ordering inside `boot()`'s own `catch`, so the call
  // is counted instead: zero means the guard really came first.
  const calls = [];
  const host = {
    api: (path) => {
      calls.push(path);
      return Promise.reject(new Error("unreachable"));
    },
  };
  const panel = createAgentPanel({ host });
  assert.equal(typeof panel.boot, "function");
  assert.equal(typeof panel.bind, "function");
  assert.equal(typeof panel.submit, "function");
  assert.equal(panel.online, false);
  await panel.boot();
  panel.bind();
  await panel.submit();
  assert.deepEqual(calls, [], "the drawer must not talk to the host before it has a document");
  assert.equal(panel.online, false);
});
