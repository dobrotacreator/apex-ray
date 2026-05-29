import assert from "node:assert/strict";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import ts from "typescript";

import {
  importedBindingsForTarget,
  isIdentifierMatchedByImportedBindings,
} from "../dist/workspace-import-bindings.js";
import type {
  CollectedSymbol,
  ExportedNamesForTarget,
  IdentifierIndexEntry,
  ImportIndexEntry,
  PackageInfo,
  Reference,
  RepoFileIndexEntry,
} from "../dist/types.js";

test("workspace import bindings resolve default, named, namespace, and namespace re-export imports", () => {
  const repo = path.join(os.tmpdir(), "apex-ray-ts-workspace-import-bindings");
  const targetPath = path.join(repo, "src/cart.ts");
  const barrelPath = path.join(repo, "src/barrel.ts");
  const consumerPath = path.join(repo, "src/consumer.ts");
  const source = ts.createSourceFile(targetPath, "export class CartService {}\n", ts.ScriptTarget.ES2022, true, ts.ScriptKind.TS);
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
  const exportedNames: ExportedNamesForTarget = {
    allNames: new Set(["CartService", "PublicCart", "default"]),
    byFile: new Map([
      [path.resolve(targetPath), new Set(["CartService", "default"])],
      [path.resolve(barrelPath), new Set(["PublicCart"])],
    ]),
    namespacesByFile: new Map([[path.resolve(barrelPath), new Map([["CartApi", new Set(["PublicCart"])]])]]),
  };
  const consumer = fileEntry(consumerPath, "src/consumer.ts", [
    importEntry("./cart.js", { defaultImport: { localName: "DefaultCart", reference: reference("import DefaultCart") } }),
    importEntry("./cart.js", { namespaceImport: { localName: "CartNamespace", reference: reference("import * as CartNamespace") } }),
    importEntry("./barrel.js", {
      namedImports: [
        { importedName: "PublicCart", localName: "RenamedCart", reference: reference("import { PublicCart as RenamedCart }") },
        { importedName: "CartApi", localName: "Api", reference: reference("import { CartApi as Api }") },
      ],
    }),
  ]);

  const bindings = importedBindingsForTarget(consumer, repo, target, targetPackage, exportedNames);

  assert.equal(bindings.localNames.has("DefaultCart"), true);
  assert.equal(bindings.localNames.has("RenamedCart"), true);
  assert.equal(bindings.namespaceLocalNames.has("CartNamespace"), true);
  assert.equal(bindings.namespaceExportNames.get("CartNamespace")?.has("CartService"), true);
  assert.equal(bindings.namespaceLocalNames.has("Api"), true);
  assert.equal(bindings.namespaceExportNames.get("Api")?.has("PublicCart"), true);
  assert.equal(isIdentifierMatchedByImportedBindings(identifier("CartService", "CartNamespace"), bindings), true);
  assert.equal(isIdentifierMatchedByImportedBindings(identifier("PublicCart", "Api"), bindings), true);
  assert.equal(isIdentifierMatchedByImportedBindings(identifier("Other", "Api"), bindings), false);
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

function fileEntry(absPath: string, relPath: string, imports: ImportIndexEntry[]): RepoFileIndexEntry {
  return {
    absPath,
    relPath,
    relLower: relPath.toLowerCase(),
    size: 0,
    mtimeMs: 0,
    imports,
    exports: [],
    identifiers: [],
    receivers: [],
    typeAliases: [],
    classHeritages: [],
    diProviders: [],
    diInjections: [],
  };
}

function importEntry(moduleSpecifier: string, overrides: Partial<Omit<ImportIndexEntry, "moduleSpecifier">>): ImportIndexEntry {
  return {
    moduleSpecifier,
    defaultImport: overrides.defaultImport ?? null,
    namespaceImport: overrides.namespaceImport ?? null,
    namedImports: overrides.namedImports ?? [],
  };
}

function identifier(name: string, namespaceQualifier: string | null): IdentifierIndexEntry {
  return {
    name,
    namespaceQualifier,
    reference: reference(`${namespaceQualifier ? `${namespaceQualifier}.` : ""}${name}`),
  };
}

function reference(text: string): Reference {
  return {
    file: "src/consumer.ts",
    line: 1,
    text,
    kind: "import",
  };
}
