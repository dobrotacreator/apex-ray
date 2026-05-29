import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import ts from "typescript";

import {
  collectDecoratorMetadataKeyConsumerContracts,
  collectFrameworkMetadata,
} from "../dist/contract-metadata.js";
import { collectSymbols } from "../dist/symbol-collection.js";
import type { Reference } from "../dist/types.js";
import { writeFile } from "./helpers.js";

test("contract metadata links decorator metadata keys to reflector consumers", () => {
  const repo = fs.mkdtempSync(path.join(os.tmpdir(), "apex-ray-ts-contract-metadata-"));
  try {
    const controllerPath = path.join(repo, "src/controller.ts");
    const decoratorsPath = path.join(repo, "src/decorators.ts");
    const guardPath = path.join(repo, "src/guard.ts");
    writeFile(
      repo,
      "src/decorators.ts",
      [
        "export const ROLE_KEY = 'role';",
        "declare function SetMetadata(key: unknown, value: unknown): MethodDecorator;",
        "export function Roles(): MethodDecorator {",
        "  return SetMetadata(ROLE_KEY, true);",
        "}",
      ].join("\n"),
    );
    writeFile(
      repo,
      "src/controller.ts",
      [
        "import { Roles } from './decorators.js';",
        "declare function Controller(): ClassDecorator;",
        "declare function Body(): ParameterDecorator;",
        "@Controller()",
        "export class CartController {",
        "  @Roles()",
        "  checkout(@Body() input: unknown): void {}",
        "}",
      ].join("\n"),
    );
    writeFile(
      repo,
      "src/guard.ts",
      [
        "import { ROLE_KEY } from './decorators.js';",
        "export class RolesGuard {",
        "  constructor(private reflector: { get(key: unknown, handler: unknown): unknown }) {}",
        "  canActivate(handler: unknown): boolean {",
        "    return Boolean(this.reflector.get(ROLE_KEY, handler));",
        "  }",
        "}",
      ].join("\n"),
    );

    const program = ts.createProgram({
      rootNames: [controllerPath, decoratorsPath, guardPath],
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

    const refs: Reference[] = [];
    collectDecoratorMetadataKeyConsumerContracts(refs, new Set(), program, program.getTypeChecker(), target, repo, 20);
    const metadata = collectFrameworkMetadata(target, repo, 20);

    assert.ok(refs.some((reference) => reference.file === "src/guard.ts" && reference.text.includes("export class RolesGuard")));
    assert.ok(metadata.some((reference) => reference.text.includes("@Controller()")));
    assert.ok(metadata.some((reference) => reference.text.includes("@Roles()")));
    assert.ok(metadata.some((reference) => reference.text.includes("@Body()")));
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
  }
});
