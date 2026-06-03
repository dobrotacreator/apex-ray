import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import ts from "typescript";

import {
  collectCallees,
  collectImplementedMemberUsageReferences,
  collectReferenceConsumerImpact,
  collectReferences,
} from "../dist/references/analysis.js";
import { collectSymbols } from "../dist/symbols/collection.js";
import { writeFile } from "./helpers.js";

test("reference analysis captures direct refs, implemented members, callees, and consumer impact", () => {
  const repo = fs.mkdtempSync(path.join(os.tmpdir(), "apex-ray-ts-reference-analysis-"));
  try {
    const cartPath = path.join(repo, "src/cart.ts");
    const contractsPath = path.join(repo, "src/contracts.ts");
    const checkoutPath = path.join(repo, "src/checkout.ts");
    const routesPath = path.join(repo, "src/routes.ts");
    const appPath = path.join(repo, "src/app.ts");
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
    writeFile(repo, "src/contracts.ts", "export interface CartPort {\n  total(): number;\n}\n");
    writeFile(
      repo,
      "src/cart.ts",
      [
        "import type { CartPort } from './contracts.js';",
        "",
        "export function helper(): number {",
        "  return 1;",
        "}",
        "",
        "export class CartService implements CartPort {",
        "  total(): number {",
        "    return helper();",
        "  }",
        "}",
      ].join("\n"),
    );
    writeFile(
      repo,
      "src/checkout.ts",
      [
        "import { CartService } from './cart.js';",
        "import type { CartPort } from './contracts.js';",
        "",
        "const service: CartPort = new CartService();",
        "",
        "export function checkout(): number {",
        "  return service.total();",
        "}",
      ].join("\n"),
    );
    writeFile(
      repo,
      "src/routes.ts",
      [
        "export const routes = [{ method: 'GET', template: '/cart' }];",
        "",
        "export function createRouter(): unknown {",
        "  return routes;",
        "}",
      ].join("\n"),
    );
    writeFile(repo, "src/app.ts", "import { createRouter } from './routes.js';\nexport function boot() {\n  return createRouter();\n}\n");

    const program = ts.createProgram({
      rootNames: [cartPath, contractsPath, checkoutPath, routesPath, appPath],
      options: {
        target: ts.ScriptTarget.ES2022,
        module: ts.ModuleKind.NodeNext,
        moduleResolution: ts.ModuleResolutionKind.NodeNext,
        strict: true,
      },
    });
    const checker = program.getTypeChecker();
    const cartSource = program.getSourceFile(cartPath);
    const routesSource = program.getSourceFile(routesPath);
    assert.ok(cartSource);
    assert.ok(routesSource);

    const cartSymbols = collectSymbols(cartSource, checker);
    const classTarget = cartSymbols.find((symbol) => symbol.analysis.name === "CartService");
    const methodTarget = cartSymbols.find((symbol) => symbol.analysis.name === "total");
    assert.ok(classTarget);
    assert.ok(methodTarget);

    const classRefs = collectReferences(program, checker, classTarget, repo, 20);
    const implementedMemberRefs = collectImplementedMemberUsageReferences(program, checker, methodTarget, repo, 20);
    const callees = collectCallees(checker, methodTarget, repo, 20);

    assert.ok(classRefs.some((reference) => reference.text.includes("new CartService()")));
    assert.ok(implementedMemberRefs.some((reference) => reference.text.includes("service.total()")));
    assert.ok(callees.some((reference) => reference.file === "src/cart.ts" && reference.text.includes("helper(): number")));

    const routeTarget = collectSymbols(routesSource, checker).find((symbol) => symbol.analysis.name === "routes:GET /cart");
    assert.ok(routeTarget);
    const consumerImpact = collectReferenceConsumerImpact(program, checker, routeTarget, repo, 20);
    assert.ok(consumerImpact.references.some((reference) => reference.file === "src/app.ts" && reference.text.includes("createRouter()")));
    assert.ok(consumerImpact.callees.some((reference) => reference.file === "src/routes.ts" && reference.text.includes("createRouter")));
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
  }
});
