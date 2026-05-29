import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import ts from "typescript";

import { parseArgs } from "../dist/cli.js";
import { buildRepoIndex } from "../dist/indexes/repo-index.js";
import { collectSymbols } from "../dist/symbols/symbol-collection.js";
import type { Reference } from "../dist/types.js";
import {
  collectWorkspaceImportReferences,
  collectWorkspaceMemberReferences,
  filterInvalidWorkspaceMemberReferences,
} from "../dist/workspace/workspace-references.js";
import { writeFile } from "./helpers.js";

test("workspace references capture imports and filter unrelated member receivers", () => {
  const repo = fs.mkdtempSync(path.join(os.tmpdir(), "apex-ray-ts-workspace-refs-"));
  try {
    const filePath = path.join(repo, "src/cart.ts");
    writeFile(repo, "package.json", JSON.stringify({ name: "workspace" }));
    writeFile(
      repo,
      "tsconfig.json",
      JSON.stringify({
        compilerOptions: {
          target: "ES2022",
          module: "NodeNext",
          moduleResolution: "NodeNext",
          strict: true,
        },
        include: ["src/**/*.ts"],
      }),
    );
    writeFile(
      repo,
      "src/cart.ts",
      [
        "export class CartService {",
        "  total(): number {",
        "    return 1;",
        "  }",
        "}",
      ].join("\n"),
    );
    writeFile(
      repo,
      "src/checkout.ts",
      [
        "import { CartService } from './cart.js';",
        "",
        "const service = new CartService();",
        "service.total();",
        "const other = { total: () => 0 };",
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

    const repoIndex = buildRepoIndex(parseArgs(["--repo", repo, "--changed", "src/cart.ts", "--no-index-cache"]));
    const symbols = collectSymbols(source, program.getTypeChecker());
    const classTarget = symbols.find((symbol) => symbol.analysis.name === "CartService");
    const methodTarget = symbols.find((symbol) => symbol.analysis.name === "total");
    assert.ok(classTarget);
    assert.ok(methodTarget);

    const importRefs = collectWorkspaceImportReferences(repo, repoIndex, classTarget, 20);
    assert.ok(importRefs.some((reference) => reference.file === "src/checkout.ts" && reference.kind === "import"));
    assert.ok(importRefs.some((reference) => reference.file === "src/checkout.ts" && reference.text.includes("new CartService()")));

    const memberRefs = collectWorkspaceMemberReferences(repo, repoIndex, methodTarget, 20);
    const unrelatedMemberRef: Reference = {
      file: "src/checkout.ts",
      line: 6,
      text: "other.total();",
      kind: "call",
    };
    const filteredMemberRefs = filterInvalidWorkspaceMemberReferences(
      repo,
      repoIndex,
      methodTarget,
      [...memberRefs, unrelatedMemberRef],
    );

    assert.ok(memberRefs.some((reference) => reference.text.includes("service.total()")));
    assert.equal(filteredMemberRefs.some((reference) => reference.text.includes("service.total()")), true);
    assert.equal(filteredMemberRefs.some((reference) => reference.text.includes("other.total()")), false);
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
  }
});
