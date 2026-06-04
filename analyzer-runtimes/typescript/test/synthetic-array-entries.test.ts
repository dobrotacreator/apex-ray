import assert from "node:assert/strict";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import ts from "typescript";

import { arrayEntrySymbolName } from "../dist/symbols/synthetic-array-entries.js";

test("synthetic array entry names prefer route tuple, object ids, primitives, and fallbacks", () => {
  const filePath = path.join(os.tmpdir(), "apex-ray-ts-synthetic-array-entries.ts");
  const source = ts.createSourceFile(
    filePath,
    [
      "export const routes = [",
      "  { method: 'GET', template: '/cart' },",
      "  ['POST', '/cart'],",
      "  'health',",
      "  { permission: 'cart.read' },",
      "  { nested: true },",
      "];",
    ].join("\n"),
    ts.ScriptTarget.ES2022,
    true,
    ts.ScriptKind.TS,
  );
  const array = arrayLiteralNamed(source, "routes");
  const entries = array.elements.filter((element): element is ts.Expression => !ts.isSpreadElement(element));

  assert.equal(arrayEntrySymbolName("routes", entries[0], 0, source), "routes:GET /cart");
  assert.equal(arrayEntrySymbolName("routes", entries[1], 1, source), "routes:POST /cart");
  assert.equal(arrayEntrySymbolName("routes", entries[2], 2, source), "routes:health");
  assert.equal(arrayEntrySymbolName("routes", entries[3], 3, source), "routes:cart.read");
  assert.equal(arrayEntrySymbolName("routes", entries[4], 4, source), "routes:entry-5");
});

function arrayLiteralNamed(source: ts.SourceFile, name: string): ts.ArrayLiteralExpression {
  for (const statement of source.statements) {
    if (!ts.isVariableStatement(statement)) continue;
    const declaration = statement.declarationList.declarations[0];
    if (!declaration || !ts.isIdentifier(declaration.name) || declaration.name.text !== name) continue;
    if (declaration.initializer && ts.isArrayLiteralExpression(declaration.initializer)) {
      return declaration.initializer;
    }
  }
  throw new Error(`Missing array ${name}`);
}
