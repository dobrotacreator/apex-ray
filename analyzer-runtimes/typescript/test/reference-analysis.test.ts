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
  ReferenceScanCancelled,
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

test("reference analysis keeps semantic references when raw text prefilters are unsafe", () => {
  const repo = fs.mkdtempSync(path.join(os.tmpdir(), "apex-ray-ts-reference-prefilter-"));
  try {
    const defaultsPath = path.join(repo, "src/defaults.ts");
    const defaultConsumerPath = path.join(repo, "src/default-consumer.ts");
    const escapedPath = path.join(repo, "src/escaped.ts");
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
    writeFile(repo, "src/defaults.ts", "export default function checkout(): number {\n  return 1;\n}\n");
    writeFile(repo, "src/default-consumer.ts", "import run from './defaults.js';\nexport const value = run();\n");
    writeFile(
      repo,
      "src/escaped.ts",
      [
        "export class Cart {",
        "  checkout(): number {",
        "    return 1;",
        "  }",
        "}",
        "",
        "const cart = new Cart();",
        "export const escapedValue = cart.\\u0063heckout();",
      ].join("\n"),
    );

    const program = ts.createProgram({
      rootNames: [defaultsPath, defaultConsumerPath, escapedPath],
      options: {
        target: ts.ScriptTarget.ES2022,
        module: ts.ModuleKind.NodeNext,
        moduleResolution: ts.ModuleResolutionKind.NodeNext,
        strict: true,
      },
    });
    const checker = program.getTypeChecker();
    const defaultsSource = program.getSourceFile(defaultsPath);
    const escapedSource = program.getSourceFile(escapedPath);
    assert.ok(defaultsSource);
    assert.ok(escapedSource);

    const defaultTarget = collectSymbols(defaultsSource, checker).find((symbol) => symbol.analysis.name === "checkout");
    const methodTarget = collectSymbols(escapedSource, checker).find((symbol) => symbol.analysis.name === "checkout");
    assert.ok(defaultTarget);
    assert.ok(methodTarget);

    const defaultRefs = collectReferences(program, checker, defaultTarget, repo, 20);
    const escapedRefs = collectReferences(program, checker, methodTarget, repo, 20);

    assert.ok(defaultRefs.some((reference) => reference.file === "src/default-consumer.ts" && reference.text.includes("run()")));
    assert.ok(escapedRefs.some((reference) => reference.file === "src/escaped.ts" && reference.text.includes("\\u0063heckout")));
    assert.throws(() => collectReferences(program, checker, defaultTarget, repo, 20, () => true), ReferenceScanCancelled);
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
  }
});

test("reference analysis follows aliased re-export consumers", () => {
  const repo = fs.mkdtempSync(path.join(os.tmpdir(), "apex-ray-ts-reference-alias-"));
  try {
    const fooPath = path.join(repo, "src/foo.ts");
    const barrelPath = path.join(repo, "src/barrel.ts");
    const consumerPath = path.join(repo, "src/consumer.ts");
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
    writeFile(repo, "src/foo.ts", "export function Foo(): number {\n  return 1;\n}\n");
    writeFile(repo, "src/barrel.ts", "export { Foo as Bar } from './foo.js';\n");
    writeFile(repo, "src/consumer.ts", "import { Bar } from './barrel.js';\nexport const value = Bar();\n");

    const program = ts.createProgram({
      rootNames: [fooPath, barrelPath, consumerPath],
      options: {
        target: ts.ScriptTarget.ES2022,
        module: ts.ModuleKind.NodeNext,
        moduleResolution: ts.ModuleResolutionKind.NodeNext,
        strict: true,
      },
    });
    const checker = program.getTypeChecker();
    const fooSource = program.getSourceFile(fooPath);
    assert.ok(fooSource);

    const target = collectSymbols(fooSource, checker).find((symbol) => symbol.analysis.name === "Foo");
    assert.ok(target);

    const refs = collectReferences(program, checker, target, repo, 20);

    assert.ok(refs.some((reference) => reference.file === "src/consumer.ts" && reference.text.includes("Bar()")));
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
  }
});
