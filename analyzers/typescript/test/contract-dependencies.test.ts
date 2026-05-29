import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import ts from "typescript";

import {
  addContractSymbolWithDependencies,
  isContractDependencyIdentifier,
} from "../dist/contract-dependencies.js";
import type { Reference } from "../dist/types.js";
import { writeFile } from "./helpers.js";

test("contract dependencies expand Pick<typeof value> into picked property references", () => {
  const repo = fs.mkdtempSync(path.join(os.tmpdir(), "apex-ray-ts-contract-dependencies-"));
  try {
    const filePath = path.join(repo, "src/contracts.ts");
    writeFile(
      repo,
      "src/contracts.ts",
      [
        "export const AddressDefaults = {",
        "  street: 'main',",
        "  zip: '10000',",
        "};",
        "",
        "export type AddressPatch = Pick<typeof AddressDefaults, 'street'>;",
        "",
        "export function useAddress(input: AddressPatch): AddressPatch {",
        "  return input;",
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
    const checker = program.getTypeChecker();
    const source = program.getSourceFile(filePath);
    assert.ok(source);
    const target = functionNamed(source, "useAddress");
    const typeAliasName = typeAliasNamed(source, "AddressPatch").name;
    const symbol = checker.getSymbolAtLocation(typeAliasName);
    const refs: Reference[] = [];

    addContractSymbolWithDependencies(refs, new Set(), new Set(), checker, symbol ?? null, target, repo, 20, 0);

    assert.ok(refs.some((reference) => reference.text.includes("export type AddressPatch")));
    assert.ok(refs.some((reference) => reference.text.includes("export const AddressDefaults")));
    assert.ok(refs.some((reference) => reference.text.includes("street: 'main'")));
    assert.equal(refs.some((reference) => reference.text.includes("zip: '10000'")), false);
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
  }
});

test("contract dependency identifier filter ignores declarations and property names", () => {
  const source = ts.createSourceFile(
    "contract-dependency-identifiers.ts",
    [
      "import { Imported } from './imported';",
      "const source = { value: External.value };",
      "const target = source.value;",
    ].join("\n"),
    ts.ScriptTarget.ES2022,
    true,
    ts.ScriptKind.TS,
  );

  assert.equal(isContractDependencyIdentifier(identifierNamed(source, "Imported")), false);
  assert.equal(isContractDependencyIdentifier(identifierNamed(source, "source")), false);
  assert.equal(isContractDependencyIdentifier(identifierNamed(source, "value")), false);
  assert.equal(isContractDependencyIdentifier(identifierNamed(source, "External")), true);
});

function functionNamed(source: ts.SourceFile, name: string): ts.FunctionDeclaration {
  for (const statement of source.statements) {
    if (ts.isFunctionDeclaration(statement) && statement.name?.text === name) {
      return statement;
    }
  }
  throw new Error(`Missing function ${name}`);
}

function typeAliasNamed(source: ts.SourceFile, name: string): ts.TypeAliasDeclaration {
  for (const statement of source.statements) {
    if (ts.isTypeAliasDeclaration(statement) && statement.name.text === name) {
      return statement;
    }
  }
  throw new Error(`Missing type alias ${name}`);
}

function identifierNamed(source: ts.SourceFile, name: string): ts.Identifier {
  let found: ts.Identifier | null = null;
  visit(source);
  if (!found) throw new Error(`Missing identifier ${name}`);
  return found;

  function visit(node: ts.Node): void {
    if (found) return;
    if (ts.isIdentifier(node) && node.text === name) {
      found = node;
      return;
    }
    ts.forEachChild(node, visit);
  }
}
