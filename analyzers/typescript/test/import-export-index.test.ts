import assert from "node:assert/strict";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import ts from "typescript";

import { STAR_EXPORT_LOCAL_NAME } from "../dist/constants.js";
import { collectExportIndex, collectImportIndex, commonJsExportEntries } from "../dist/import-export-index.js";

test("import/export index captures module syntaxes and CommonJS exports", () => {
  const repo = path.join(os.tmpdir(), "apex-ray-ts-import-export-index");
  const absPath = path.join(repo, "src/module.ts");
  const text = [
    "import DefaultThing, { Named as LocalNamed } from './dep.js';",
    "import * as Namespace from './namespace.js';",
    "import Fs = require('node:fs');",
    "const { readFile: readFileLocal } = require('node:fs');",
    "const statLocal = require('node:fs').stat;",
    "const dynamic = await import('./lazy.js');",
    "const { loaded: loadedLocal } = await import('./loaded.js');",
    "export * from './all.js';",
    "export * as Lib from './lib.js';",
    "export { LocalNamed as ExportedNamed } from './dep.js';",
    "module.exports = { default: DefaultThing, LocalNamed };",
    "exports.extra = LocalNamed;",
    "module.exports['quoted'] = LocalNamed;",
  ].join("\n");
  const source = ts.createSourceFile(absPath, text, ts.ScriptTarget.ES2022, true, ts.ScriptKind.TS);

  const imports = collectImportIndex(repo, source);
  const exports = collectExportIndex(repo, source);
  const commonJsExports = source.statements.flatMap((statement) =>
    ts.isExpressionStatement(statement) ? commonJsExportEntries(statement.expression) : [],
  );

  assert.ok(imports.some((item) => item.moduleSpecifier === "./dep.js" && item.defaultImport?.localName === "DefaultThing"));
  assert.ok(imports.some((item) => item.moduleSpecifier === "./dep.js" && item.namedImports[0]?.localName === "LocalNamed"));
  assert.ok(imports.some((item) => item.moduleSpecifier === "./namespace.js" && item.namespaceImport?.localName === "Namespace"));
  assert.ok(imports.some((item) => item.moduleSpecifier === "node:fs" && item.defaultImport?.localName === "Fs"));
  assert.ok(imports.some((item) => item.moduleSpecifier === "node:fs" && item.namedImports[0]?.localName === "readFileLocal"));
  assert.ok(imports.some((item) => item.moduleSpecifier === "node:fs" && item.namedImports[0]?.localName === "statLocal"));
  assert.ok(imports.some((item) => item.moduleSpecifier === "./lazy.js" && item.namespaceImport?.localName === "dynamic"));
  assert.ok(imports.some((item) => item.moduleSpecifier === "./loaded.js" && item.namedImports[0]?.localName === "loadedLocal"));

  assert.ok(exports.some((item) => item.moduleSpecifier === "./all.js" && item.exportedName === STAR_EXPORT_LOCAL_NAME));
  assert.ok(exports.some((item) => item.moduleSpecifier === "./lib.js" && item.exportedName === "Lib"));
  assert.ok(exports.some((item) => item.moduleSpecifier === "./dep.js" && item.exportedName === "ExportedNamed"));
  assert.ok(exports.some((item) => item.moduleSpecifier === null && item.localName === "DefaultThing" && item.exportedName === "default"));
  assert.ok(exports.some((item) => item.moduleSpecifier === null && item.localName === "LocalNamed" && item.exportedName === "extra"));
  assert.ok(exports.some((item) => item.moduleSpecifier === null && item.localName === "LocalNamed" && item.exportedName === "quoted"));

  assert.ok(commonJsExports.some((item) => item.localName === "DefaultThing" && item.defaultExported));
  assert.ok(commonJsExports.some((item) => item.localName === "LocalNamed" && item.exportedName === "quoted"));
});
