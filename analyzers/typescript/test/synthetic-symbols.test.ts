import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import ts from "typescript";

import {
  collectConstArrayEntrySymbols,
  collectConstObjectMemberSymbols,
  collectDeletedSymbols,
  collectEnumMemberSymbols,
  collectFactoryCallArrayEntrySymbols,
  preferSyntheticChildSymbols,
} from "../dist/synthetic-symbols.js";
import type { CollectedSymbol, SymbolKind } from "../dist/types.js";
import { writeFile } from "./helpers.js";

test("synthetic symbols capture child entries and deleted child symbols", () => {
  const repo = fs.mkdtempSync(path.join(os.tmpdir(), "apex-ray-ts-synthetic-symbols-"));
  try {
    const filePath = path.join(repo, "src/routes.ts");
    writeFile(
      repo,
      "src/routes.ts",
      [
        "function helper(): number { return 1; }",
        "function save(): number { return helper(); }",
        "export const handlers = Object.freeze({",
        "  read: () => helper(),",
        "  save,",
        "});",
        "export const routes = [",
        "  { method: 'GET', template: '/cart' },",
        "  ['POST', '/cart'],",
        "  'health',",
        "  { permission: 'cart.read' },",
        "];",
        "export const factoryRoutes = defineRoutes(",
        "  [{ path: '/factory' }],",
        "  [['DELETE', '/cart']],",
        ");",
        "export enum Status {",
        "  Active = 'active',",
        "  Paused = 'paused',",
        "}",
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

    const handlers = variableStatementNamed(source, "handlers");
    const routes = variableStatementNamed(source, "routes");
    const factoryRoutes = variableStatementNamed(source, "factoryRoutes");
    const status = enumDeclarationNamed(source, "Status");

    const objectMembers = collectConstObjectMemberSymbols(handlers, checker, source, "handlers", false);
    const routeEntries = collectConstArrayEntrySymbols(routes, source, "routes", true, false, symbolForVariable(checker, routes));
    const factoryEntries = collectFactoryCallArrayEntrySymbols(
      factoryRoutes,
      source,
      "factoryRoutes",
      true,
      false,
      symbolForVariable(checker, factoryRoutes),
    );
    const enumMembers = collectEnumMemberSymbols(status, checker, source, "Status", true, false);

    assert.ok(objectMembers.some((symbol) => symbol.analysis.name === "read" && symbol.analysis.signature.includes("read: () => helper()")));
    assert.ok(objectMembers.some((symbol) => symbol.analysis.name === "save"));
    assert.ok(routeEntries.some((symbol) => symbol.analysis.name === "routes:GET /cart"));
    assert.ok(routeEntries.some((symbol) => symbol.analysis.name === "routes:POST /cart"));
    assert.ok(routeEntries.some((symbol) => symbol.analysis.name === "routes:health"));
    assert.ok(routeEntries.some((symbol) => symbol.analysis.name === "routes:cart.read"));
    assert.ok(factoryEntries.some((symbol) => symbol.analysis.name === "factoryRoutes:/factory"));
    assert.ok(factoryEntries.some((symbol) => symbol.analysis.name === "factoryRoutes:DELETE /cart"));
    assert.ok(enumMembers.some((symbol) => symbol.analysis.name === "Active" && symbol.analysis.signature === "Status.Active = 'active'"));
    assert.ok(enumMembers.some((symbol) => symbol.analysis.name === "Paused"));

    const handlerContainer = containerForVariable(source, checker, handlers, "handlers");
    const routesContainer = containerForVariable(source, checker, routes, "routes");
    const statusContainer = containerForEnum(source, checker, status, "Status");
    const preferred = preferSyntheticChildSymbols([handlerContainer, ...objectMembers]);
    const deleted = collectDeletedSymbols(source, [handlerContainer, routesContainer, statusContainer], [
      { line: handlerContainer.analysis.startLine + 1, text: "  remove: () => helper()," },
      { line: routesContainer.analysis.startLine + 1, text: "  { method: 'PATCH', template: '/cart' }," },
      { line: statusContainer.analysis.startLine + 1, text: "  Removed = 'removed'," },
    ]);

    assert.equal(preferred.some((symbol) => symbol.analysis.name === "handlers"), false);
    assert.ok(preferred.some((symbol) => symbol.analysis.name === "read"));
    assert.ok(deleted.some((symbol) => symbol.analysis.name === "remove" && symbol.analysis.signature.includes("handlers removed entry")));
    assert.ok(deleted.some((symbol) => symbol.analysis.name === "routes:PATCH /cart"));
    assert.ok(deleted.some((symbol) => symbol.analysis.name === "Removed" && symbol.analysis.kind === "enum-member"));
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
  }
});

function variableStatementNamed(source: ts.SourceFile, name: string): ts.VariableStatement {
  for (const statement of source.statements) {
    if (!ts.isVariableStatement(statement)) continue;
    const declaration = statement.declarationList.declarations[0];
    if (declaration && ts.isIdentifier(declaration.name) && declaration.name.text === name) {
      return statement;
    }
  }
  throw new Error(`Missing variable statement ${name}`);
}

function enumDeclarationNamed(source: ts.SourceFile, name: string): ts.EnumDeclaration {
  for (const statement of source.statements) {
    if (ts.isEnumDeclaration(statement) && statement.name.text === name) {
      return statement;
    }
  }
  throw new Error(`Missing enum declaration ${name}`);
}

function symbolForVariable(checker: ts.TypeChecker, statement: ts.VariableStatement): ts.Symbol | null {
  const name = statement.declarationList.declarations[0]?.name;
  return name ? checker.getSymbolAtLocation(name) ?? null : null;
}

function containerForVariable(
  source: ts.SourceFile,
  checker: ts.TypeChecker,
  statement: ts.VariableStatement,
  name: string,
): CollectedSymbol {
  return containerSymbol(source, statement, name, "variable", symbolForVariable(checker, statement));
}

function containerForEnum(
  source: ts.SourceFile,
  checker: ts.TypeChecker,
  declaration: ts.EnumDeclaration,
  name: string,
): CollectedSymbol {
  return containerSymbol(source, declaration, name, "enum", checker.getSymbolAtLocation(declaration.name) ?? null);
}

function containerSymbol(
  source: ts.SourceFile,
  node: ts.Node,
  name: string,
  kind: SymbolKind,
  tsSymbol: ts.Symbol | null,
): CollectedSymbol {
  const startLine = source.getLineAndCharacterOfPosition(node.getStart(source)).line + 1;
  const endLine = source.getLineAndCharacterOfPosition(node.getEnd()).line + 1;
  return {
    analysis: {
      name,
      kind,
      startLine,
      endLine,
      exported: true,
      signature: name,
      references: [],
      callees: [],
      contracts: [],
      metadata: [],
    },
    node,
    tsSymbol,
    defaultExported: false,
    exportContainer: null,
  };
}
