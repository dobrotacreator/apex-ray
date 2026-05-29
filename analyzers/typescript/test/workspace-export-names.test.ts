import assert from "node:assert/strict";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import ts from "typescript";

import { NAMESPACE_EXPORT_LOCAL_NAME, STAR_EXPORT_LOCAL_NAME } from "../dist/constants.js";
import { exportedNamesForTarget } from "../dist/workspace/workspace-export-names.js";
import type {
  CollectedSymbol,
  ExportIndexEntry,
  PackageInfo,
  Reference,
  RepoFileIndexEntry,
  RepoIndex,
} from "../dist/types.js";

test("workspace export names propagate direct, star, and namespace re-exports", () => {
  const repo = path.join(os.tmpdir(), "apex-ray-ts-workspace-export-names");
  const cartPath = path.join(repo, "src/cart.ts");
  const barrelPath = path.join(repo, "src/barrel.ts");
  const indexPath = path.join(repo, "src/index.ts");
  const namespacePath = path.join(repo, "src/namespace.ts");
  const source = ts.createSourceFile(cartPath, "export class CartService {}\n", ts.ScriptTarget.ES2022, true, ts.ScriptKind.TS);
  const target = targetFor(classNamed(source, "CartService"), "CartService");
  const targetPackage: PackageInfo = {
    root: repo,
    name: "workspace",
    exports: null,
    main: null,
    module: null,
    types: null,
    typings: null,
  };
  const repoIndex: RepoIndex = {
    files: [
      fileEntry(cartPath, "src/cart.ts", []),
      fileEntry(barrelPath, "src/barrel.ts", [
        exportEntry("./cart.js", "CartService", "PublicCart"),
      ]),
      fileEntry(indexPath, "src/index.ts", [
        exportEntry("./barrel.js", STAR_EXPORT_LOCAL_NAME, STAR_EXPORT_LOCAL_NAME),
      ]),
      fileEntry(namespacePath, "src/namespace.ts", [
        exportEntry("./barrel.js", NAMESPACE_EXPORT_LOCAL_NAME, "CartApi"),
      ]),
    ],
    packageByFile: new Map(),
    cacheStats: null,
  };

  const names = exportedNamesForTarget(repoIndex, target, targetPackage);

  assert.deepEqual(new Set(names.allNames), new Set(["CartService", "PublicCart"]));
  assert.deepEqual(names.byFile.get(path.resolve(cartPath)), new Set(["CartService"]));
  assert.deepEqual(names.byFile.get(path.resolve(barrelPath)), new Set(["PublicCart"]));
  assert.deepEqual(names.byFile.get(path.resolve(indexPath)), new Set(["PublicCart"]));
  assert.deepEqual(names.namespacesByFile.get(path.resolve(namespacePath))?.get("CartApi"), new Set(["PublicCart"]));
});

function classNamed(source: ts.SourceFile, name: string): ts.ClassDeclaration {
  for (const statement of source.statements) {
    if (ts.isClassDeclaration(statement) && statement.name?.text === name) {
      return statement;
    }
  }
  throw new Error(`Missing class ${name}`);
}

function targetFor(node: ts.Node, name: string): CollectedSymbol {
  return {
    analysis: {
      name,
      kind: "class",
      startLine: 1,
      endLine: 1,
      exported: true,
      signature: name,
      references: [],
      callees: [],
      contracts: [],
      metadata: [],
    },
    node,
    tsSymbol: null,
    defaultExported: false,
    exportContainer: null,
  };
}

function fileEntry(absPath: string, relPath: string, exports: ExportIndexEntry[]): RepoFileIndexEntry {
  return {
    absPath,
    relPath,
    relLower: relPath.toLowerCase(),
    size: 0,
    mtimeMs: 0,
    imports: [],
    exports,
    identifiers: [],
    receivers: [],
    typeAliases: [],
    classHeritages: [],
    diProviders: [],
    diInjections: [],
  };
}

function exportEntry(moduleSpecifier: string, localName: string, exportedName: string): ExportIndexEntry {
  return {
    moduleSpecifier,
    localName,
    exportedName,
    reference: reference(),
  };
}

function reference(): Reference {
  return {
    file: "src/index.ts",
    line: 1,
    text: "",
    kind: "import",
  };
}
