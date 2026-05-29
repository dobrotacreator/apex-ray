import assert from "node:assert/strict";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import ts from "typescript";

import { collectExportedSymbolInfo, collectExports, collectImports } from "../dist/symbol-export-info.js";

test("symbol export info captures summaries and local exported names", () => {
  const filePath = path.join(os.tmpdir(), "apex-ray-ts-symbol-export-info.ts");
  const text = [
    "import DefaultThing, { Named as LocalNamed } from './dep.js';",
    "import Fs = require('node:fs');",
    "const Hidden = 1;",
    "const Renamed = 2;",
    "const DefaultThing2 = 3;",
    "const CjsDefault = 4;",
    "const CjsNamed = 5;",
    "export { Renamed as PublicName, DefaultThing2 as default };",
    "export const Direct = 6;",
    "export default Hidden;",
    "module.exports.default = CjsDefault;",
    "exports.extra = CjsNamed;",
  ].join("\n");
  const source = ts.createSourceFile(filePath, text, ts.ScriptTarget.ES2022, true, ts.ScriptKind.TS);

  const imports = collectImports(source);
  const exports = collectExports(source);
  const exportInfo = collectExportedSymbolInfo(source);

  assert.deepEqual(imports, ["import DefaultThing, { Named as LocalNamed } from './dep.js';"]);
  assert.ok(exports.some((line) => line === "export { Renamed as PublicName, DefaultThing2 as default };"));
  assert.ok(exports.some((line) => line === "export const Direct = 6;"));
  assert.ok(exports.some((line) => line === "export default Hidden;"));
  assert.ok(exports.some((line) => line === "module.exports.default = CjsDefault;"));
  assert.ok(exports.some((line) => line === "exports.extra = CjsNamed;"));
  assert.equal(exportInfo.named.has("Renamed"), true);
  assert.equal(exportInfo.named.has("CjsNamed"), true);
  assert.equal(exportInfo.defaultNames.has("DefaultThing2"), true);
  assert.equal(exportInfo.defaultNames.has("Hidden"), true);
  assert.equal(exportInfo.defaultNames.has("CjsDefault"), true);
});
