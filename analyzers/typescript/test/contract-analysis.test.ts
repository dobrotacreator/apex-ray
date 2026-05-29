import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import ts from "typescript";

import { collectFrameworkMetadata, collectSchemaContracts } from "../dist/contract-analysis.js";
import { collectSymbols } from "../dist/symbol-collection.js";
import { writeFile } from "./helpers.js";

test("contract analysis captures declared types, schema receivers, and framework metadata", () => {
  const repo = fs.mkdtempSync(path.join(os.tmpdir(), "apex-ray-ts-contract-analysis-"));
  try {
    const controllerPath = path.join(repo, "src/controller.ts");
    const contractsPath = path.join(repo, "src/contracts.ts");
    writeFile(
      repo,
      "tsconfig.json",
      JSON.stringify({
        compilerOptions: {
          target: "ES2022",
          module: "NodeNext",
          moduleResolution: "NodeNext",
          strict: true,
          experimentalDecorators: true,
        },
        include: ["src/**/*.ts"],
      }),
    );
    writeFile(
      repo,
      "src/contracts.ts",
      [
        "export interface CartItem {",
        "  price: number;",
        "}",
        "",
        "export const cartSchema = {",
        "  parse(value: unknown): CartItem {",
        "    return value as CartItem;",
        "  },",
        "};",
        "",
        "export function Controller(): ClassDecorator {",
        "  return () => {};",
        "}",
      ].join("\n"),
    );
    writeFile(
      repo,
      "src/controller.ts",
      [
        "import { CartItem, Controller, cartSchema } from './contracts.js';",
        "",
        "@Controller()",
        "export class CartController {",
        "  checkout(item: CartItem): CartItem {",
        "    return cartSchema.parse(item);",
        "  }",
        "}",
      ].join("\n"),
    );

    const program = ts.createProgram({
      rootNames: [controllerPath, contractsPath],
      options: {
        target: ts.ScriptTarget.ES2022,
        module: ts.ModuleKind.NodeNext,
        moduleResolution: ts.ModuleResolutionKind.NodeNext,
        strict: true,
        experimentalDecorators: true,
      },
    });
    const source = program.getSourceFile(controllerPath);
    assert.ok(source);
    const target = collectSymbols(source, program.getTypeChecker()).find((symbol) => symbol.analysis.name === "checkout");
    assert.ok(target);

    const contracts = collectSchemaContracts(program, program.getTypeChecker(), target, repo, 20);
    const metadata = collectFrameworkMetadata(target, repo, 20);

    assert.ok(
      contracts.some(
        (reference) =>
          reference.file === "src/contracts.ts" &&
          reference.kind === "contract" &&
          reference.text.includes("export interface CartItem"),
      ),
    );
    assert.ok(
      contracts.some(
        (reference) =>
          reference.file === "src/contracts.ts" &&
          reference.kind === "contract" &&
          reference.text.includes("export const cartSchema"),
      ),
    );
    assert.ok(metadata.some((reference) => reference.kind === "metadata" && reference.text.includes("@Controller()")));
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
  }
});
