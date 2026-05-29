import assert from "node:assert/strict";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import ts from "typescript";

import {
  inferredMemberIdentifiers,
  isMemberReferenceForTarget,
  memberIdentifierHasValidReceiver,
  memberReceiverTypeNames,
} from "../dist/workspace-member-receivers.js";
import type {
  CollectedSymbol,
  IdentifierIndexEntry,
  ImportedBindingsForTarget,
  Reference,
  RepoFileIndexEntry,
  RepoIndex,
} from "../dist/types.js";

test("workspace member receiver matching follows receiver aliases and namespace bindings", () => {
  const bindings: ImportedBindingsForTarget = {
    localNames: new Map([["CartService", reference("import { CartService }")]]),
    namespaceLocalNames: new Map([["CartApi", reference("import * as CartApi")]]),
    namespaceExportNames: new Map([["CartApi", new Set(["total"])]]),
  };
  const entry = fileEntry({
    receivers: [
      {
        receiverName: "service",
        typeName: "AliasCart",
        startLine: 1,
        endLine: 10,
        reference: reference("const service: AliasCart = create();"),
      },
    ],
    typeAliases: [{ name: "AliasCart", targetName: "CartService" }],
  });

  assert.equal(isMemberReferenceForTarget(identifier("total", "service", 5), "total", entry, bindings), true);
  assert.equal(isMemberReferenceForTarget(identifier("total", "CartApi", 5), "total", entry, bindings), true);
  assert.equal(isMemberReferenceForTarget(identifier("total", "unknown", 5), "total", entry, bindings), false);
  assert.equal(isMemberReferenceForTarget(identifier("total", "service", 5, "import"), "total", entry, bindings), false);
});

test("workspace member receiver validation accepts derived and qualified receiver types", () => {
  const entry = fileEntry({
    receivers: [
      {
        receiverName: "service",
        typeName: "models.AliasCart",
        startLine: 1,
        endLine: 10,
        reference: reference("const service: models.AliasCart = create();"),
      },
    ],
    typeAliases: [{ name: "models.AliasCart", targetName: "models.CartService" }],
  });
  const emptyBindings: ImportedBindingsForTarget = {
    localNames: new Map(),
    namespaceLocalNames: new Map(),
    namespaceExportNames: new Map(),
  };

  assert.equal(
    memberIdentifierHasValidReceiver(identifier("total", "service", 5), entry, emptyBindings, new Set(["CartService"]), "total"),
    true,
  );
  assert.equal(
    memberIdentifierHasValidReceiver(identifier("total", "service", 5), entry, emptyBindings, new Set(["OtherService"]), "total"),
    false,
  );
});

test("workspace member receiver names include class, heritage, and derived classes", () => {
  const filePath = path.join(os.tmpdir(), "apex-ray-ts-workspace-member-receivers.ts");
  const source = ts.createSourceFile(
    filePath,
    "export class BaseCart extends RootCart { total(): number { return 1; } }\n",
    ts.ScriptTarget.ES2022,
    true,
    ts.ScriptKind.TS,
  );
  const classDeclaration = classNamed(source, "BaseCart");
  const method = methodNamed(classDeclaration, "total");
  const repoIndex = repoIndexFor([
    fileEntry({
      classHeritages: [
        { className: "ChildCart", baseNames: ["BaseCart"] },
        { className: "GrandChildCart", baseNames: ["ChildCart"] },
      ],
    }),
  ]);

  const names = memberReceiverTypeNames(repoIndex, targetFor(method, "total"));

  assert.equal(names.has("BaseCart"), true);
  assert.equal(names.has("RootCart"), true);
  assert.equal(names.has("ChildCart"), true);
  assert.equal(names.has("GrandChildCart"), true);
});

test("workspace member receiver fallback infers member identifiers from reference text", () => {
  const identifiers = inferredMemberIdentifiers(
    {
      file: "src/cart.ts",
      line: 1,
      text: "this.total(); service.total(); nested.service.total();",
      kind: "call",
    },
    "total",
  );

  assert.deepEqual(
    identifiers.map((identifierEntry) => identifierEntry.namespaceQualifier),
    ["this", "service", "nested.service"],
  );
});

function classNamed(source: ts.SourceFile, name: string): ts.ClassDeclaration {
  for (const statement of source.statements) {
    if (ts.isClassDeclaration(statement) && statement.name?.text === name) {
      return statement;
    }
  }
  throw new Error(`Missing class ${name}`);
}

function methodNamed(declaration: ts.ClassDeclaration, name: string): ts.MethodDeclaration {
  for (const member of declaration.members) {
    if (ts.isMethodDeclaration(member) && ts.isIdentifier(member.name) && member.name.text === name) {
      return member;
    }
  }
  throw new Error(`Missing method ${name}`);
}

function targetFor(node: ts.Node, name: string): CollectedSymbol {
  return {
    analysis: {
      name,
      kind: "method",
      startLine: 1,
      endLine: 1,
      exported: false,
      signature: name,
      references: [],
      callees: [],
      contracts: [],
      metadata: [],
    },
    node,
    tsSymbol: null,
    defaultExported: false,
    exportContainer: { name: "BaseCart", defaultExported: false },
  };
}

function fileEntry(overrides: Partial<RepoFileIndexEntry> = {}): RepoFileIndexEntry {
  return {
    absPath: "/repo/src/cart.ts",
    relPath: "src/cart.ts",
    relLower: "src/cart.ts",
    size: 0,
    mtimeMs: 0,
    imports: [],
    exports: [],
    identifiers: [],
    receivers: [],
    typeAliases: [],
    classHeritages: [],
    diProviders: [],
    diInjections: [],
    ...overrides,
  };
}

function repoIndexFor(files: RepoFileIndexEntry[]): RepoIndex {
  return {
    files,
    packageByFile: new Map(),
    cacheStats: null,
  };
}

function identifier(
  name: string,
  namespaceQualifier: string | null,
  line: number,
  kind: Reference["kind"] = "call",
): IdentifierIndexEntry {
  return {
    name,
    namespaceQualifier,
    reference: {
      file: "src/cart.ts",
      line,
      text: `${namespaceQualifier ? `${namespaceQualifier}.` : ""}${name}();`,
      kind,
    },
  };
}

function reference(text: string): Reference {
  return {
    file: "src/cart.ts",
    line: 1,
    text,
    kind: "unknown",
  };
}
