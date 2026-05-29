import assert from "node:assert/strict";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import ts from "typescript";

import {
  metadataNodesForTarget,
  parametersForNode,
  returnTypeForNode,
  variableTypeNodesForTarget,
} from "../dist/contract-targets.js";
import type { CollectedSymbol, SymbolKind } from "../dist/types.js";

test("contract target helpers expose parameters, return types, variable type context, and metadata nodes", () => {
  const filePath = path.join(os.tmpdir(), "apex-ray-ts-contract-targets.ts");
  const text = [
    "@Controller()",
    "class CartController {",
    "  constructor(@Inject() private service: CartService) {}",
    "  @Post()",
    "  checkout(@Body() input: CreateCartDto): CartResult {",
    "    return input as CartResult;",
    "  }",
    "}",
    "const typed: ExplicitType = Object.freeze(({ id: 1 } as FrozenType)) satisfies SatisfiesType;",
  ].join("\n");
  const source = ts.createSourceFile(filePath, text, ts.ScriptTarget.ES2022, true, ts.ScriptKind.TS);
  const controller = classNamed(source, "CartController");
  const checkout = methodNamed(controller, "checkout");
  const typed = variableStatementNamed(source, "typed");

  assert.deepEqual(parametersForNode(controller).map((parameter) => parameter.name.getText(source)), ["service"]);
  assert.deepEqual(parametersForNode(checkout).map((parameter) => parameter.name.getText(source)), ["input"]);
  assert.equal(returnTypeForNode(checkout)?.getText(source), "CartResult");
  assert.deepEqual(
    variableTypeNodesForTarget(targetFor(typed, "typed", "variable")).map((node) => node.getText(source)),
    ["ExplicitType", "SatisfiesType", "FrozenType"],
  );

  const methodMetadataNodes = metadataNodesForTarget(targetFor(checkout, "checkout", "method")).map((node) => node.getText(source));
  assert.ok(methodMetadataNodes.some((nodeText) => nodeText.includes("class CartController")));
  assert.ok(methodMetadataNodes.some((nodeText) => nodeText.includes("checkout")));
  assert.ok(methodMetadataNodes.some((nodeText) => nodeText.includes("input: CreateCartDto")));

  const classMetadataNodes = metadataNodesForTarget(targetFor(controller, "CartController", "class")).map((node) => node.getText(source));
  assert.ok(classMetadataNodes.some((nodeText) => nodeText.includes("class CartController")));
  assert.ok(classMetadataNodes.some((nodeText) => nodeText.includes("service: CartService")));
  assert.ok(classMetadataNodes.some((nodeText) => nodeText.includes("checkout")));
  assert.ok(classMetadataNodes.some((nodeText) => nodeText.includes("input: CreateCartDto")));
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

function targetFor(node: ts.Node, name: string, kind: SymbolKind): CollectedSymbol {
  const source = node.getSourceFile();
  const startLine = source.getLineAndCharacterOfPosition(node.getStart(source)).line + 1;
  const endLine = source.getLineAndCharacterOfPosition(node.getEnd()).line + 1;
  return {
    analysis: {
      name,
      kind,
      startLine,
      endLine,
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
    exportContainer: null,
  };
}
