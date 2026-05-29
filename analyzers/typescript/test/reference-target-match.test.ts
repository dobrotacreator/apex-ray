import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import ts from "typescript";

import { isReferenceToTarget } from "../dist/reference-target-match.js";
import { collectSymbols } from "../dist/symbol-collection.js";
import { writeFile } from "./helpers.js";

test("reference target matching accepts inherited member receivers and rejects unrelated members", () => {
  const repo = fs.mkdtempSync(path.join(os.tmpdir(), "apex-ray-ts-reference-target-match-"));
  try {
    const filePath = path.join(repo, "src/cart.ts");
    writeFile(
      repo,
      "src/cart.ts",
      [
        "export class BaseCart {",
        "  total(): number { return 1; }",
        "}",
        "export class DerivedCart extends BaseCart {}",
        "export class OtherCart {",
        "  total(): number { return 2; }",
        "}",
        "const service = new DerivedCart();",
        "service.total();",
        "const other = new OtherCart();",
        "other.total();",
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
    const checker = program.getTypeChecker();
    const target = collectSymbols(source, checker).find((symbol) => symbol.analysis.name === "total");
    assert.ok(target);

    assert.equal(isReferenceToTarget(identifierOnLine(source, "total", 9), checker, target), true);
    assert.equal(isReferenceToTarget(identifierOnLine(source, "total", 11), checker, target), false);
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
  }
});

function identifierOnLine(source: ts.SourceFile, name: string, line: number): ts.Identifier {
  let found: ts.Identifier | null = null;
  visit(source);
  if (!found) throw new Error(`Missing identifier ${name} on line ${line}`);
  return found;

  function visit(node: ts.Node): void {
    if (found) return;
    if (ts.isIdentifier(node) && node.text === name) {
      const nodeLine = source.getLineAndCharacterOfPosition(node.getStart(source)).line + 1;
      if (nodeLine === line) {
        found = node;
        return;
      }
    }
    ts.forEachChild(node, visit);
  }
}
