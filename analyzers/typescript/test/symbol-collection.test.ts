import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import ts from "typescript";

import {
  collectDeletedSymbols,
  collectExports,
  collectImports,
  collectSymbols,
  preferSyntheticChildSymbols,
} from "../dist/symbols/symbol-collection.js";
import { writeFile } from "./helpers.js";

test("symbol collection captures imports, exports, synthetic symbols, and deleted entries", () => {
  const repo = fs.mkdtempSync(path.join(os.tmpdir(), "apex-ray-ts-symbol-collection-"));
  try {
    const filePath = path.join(repo, "src/routes.ts");
    writeFile(repo, "src/helper.ts", "export function helper(): number { return 1; }\n");
    writeFile(
      repo,
      "src/routes.ts",
      [
        "import { helper } from './helper.js';",
        "",
        "export const handlers = Object.freeze({",
        "  read: () => helper(),",
        "  save,",
        "});",
        "",
        "export const routes = [",
        "  { method: 'GET', template: '/cart' },",
        "  ['POST', '/cart'],",
        "];",
        "",
        "export enum Status {",
        "  Active = 'active',",
        "}",
        "",
        "function save(): number { return helper(); }",
        "module.exports.default = handlers;",
      ].join("\n"),
    );

    const program = ts.createProgram({
      rootNames: [filePath],
      options: {
        target: ts.ScriptTarget.ES2022,
        module: ts.ModuleKind.NodeNext,
        moduleResolution: ts.ModuleResolutionKind.NodeNext,
        strict: true,
      },
    });
    const source = program.getSourceFile(filePath);
    assert.ok(source);

    const symbols = collectSymbols(source, program.getTypeChecker());
    const preferredSymbols = preferSyntheticChildSymbols(symbols);
    const names = new Set(symbols.map((symbol) => symbol.analysis.name));
    const preferredNames = new Set(preferredSymbols.map((symbol) => symbol.analysis.name));
    const deletedLine = source.getLineAndCharacterOfPosition(source.text.indexOf("read:")).line + 1;
    const deletedSymbols = collectDeletedSymbols(source, symbols, [
      { line: deletedLine, text: "  remove: () => helper()," },
    ]);

    assert.deepEqual(collectImports(source), ["import { helper } from './helper.js';"]);
    assert.ok(collectExports(source).some((line) => line.includes("export const handlers")));
    assert.ok(collectExports(source).some((line) => line.includes("module.exports.default")));
    assert.ok(names.has("handlers"));
    assert.ok(names.has("read"));
    assert.ok(names.has("routes:GET /cart"));
    assert.ok(names.has("routes:POST /cart"));
    assert.ok(names.has("Status"));
    assert.ok(names.has("Active"));
    assert.equal(preferredNames.has("handlers"), false);
    assert.equal(preferredNames.has("routes"), false);
    assert.equal(preferredNames.has("Status"), false);
    assert.equal(deletedSymbols[0]?.analysis.name, "remove");
    assert.match(deletedSymbols[0]?.analysis.signature ?? "", /handlers removed entry remove/);
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
  }
});
