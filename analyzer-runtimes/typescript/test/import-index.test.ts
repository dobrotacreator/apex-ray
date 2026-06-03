import assert from "node:assert/strict";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import ts from "typescript";

import { collectImportIndex } from "../dist/indexes/imports.js";

test("import index unwraps awaited and asserted dynamic imports", () => {
  const repo = path.join(os.tmpdir(), "apex-ray-ts-import-index");
  const absPath = path.join(repo, "src/module.ts");
  const source = ts.createSourceFile(
    absPath,
    [
      "const lazy = (await import('./lazy.js') as unknown)!;",
      "const { loaded: loadedLocal } = ((await import('./loaded.js')) satisfies unknown);",
    ].join("\n"),
    ts.ScriptTarget.ES2022,
    true,
    ts.ScriptKind.TS,
  );

  const imports = collectImportIndex(repo, source);

  assert.ok(imports.some((item) => item.moduleSpecifier === "./lazy.js" && item.namespaceImport?.localName === "lazy"));
  assert.ok(imports.some((item) => item.moduleSpecifier === "./loaded.js" && item.namedImports[0]?.localName === "loadedLocal"));
});
