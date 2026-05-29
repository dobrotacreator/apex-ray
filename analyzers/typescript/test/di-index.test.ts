import assert from "node:assert/strict";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import ts from "typescript";

import { collectDiInjectionIndex, collectDiProviderIndex } from "../dist/di-index.js";

test("DI index captures provider arrays, module providers, exports, and injections", () => {
  const repo = path.join(os.tmpdir(), "apex-ray-ts-di-index");
  const absPath = path.join(repo, "src/module.ts");
  const text = [
    "const CART_TOKEN = Symbol('cart');",
    "class CartService {}",
    "class PlainService {}",
    "class ExistingService {}",
    "class Repo {}",
    "const PROVIDERS = [",
    "  PlainService,",
    "  { provide: CART_TOKEN, useClass: CartService, inject: [Repo] },",
    "  { provide: ExistingService, useExisting: CartService },",
    "];",
    "@Module({ providers: [PROVIDERS, ...PROVIDERS], exports: [CartService] })",
    "class CartModule {}",
    "function handler(@Inject(CART_TOKEN) service: CartService): void {}",
  ].join("\n");
  const source = ts.createSourceFile(absPath, text, ts.ScriptTarget.ES2022, true, ts.ScriptKind.TS);

  const providers = collectDiProviderIndex(repo, source);
  const injections = collectDiInjectionIndex(repo, source);

  assert.ok(
    providers.some(
      (item) => item.tokenName === "CART_TOKEN" && item.implementationName === "CartService" && item.sourceArrayName === "PROVIDERS",
    ),
  );
  assert.ok(
    providers.some(
      (item) => item.tokenName === "PlainService" && item.implementationName === "PlainService" && item.sourceArrayName === "PROVIDERS",
    ),
  );
  assert.ok(providers.some((item) => item.tokenName === "CART_TOKEN" && item.implementationName === "Repo"));
  assert.ok(
    providers.some(
      (item) => item.tokenName === "ExistingService" && item.implementationName === "CartService" && item.sourceArrayName === "PROVIDERS",
    ),
  );
  assert.ok(providers.some((item) => item.tokenName === "PROVIDERS" && item.implementationName === "PROVIDERS"));
  assert.ok(providers.some((item) => item.tokenName === "CartService" && item.implementationName === "CartService"));
  assert.ok(injections.some((item) => item.tokenName === "CART_TOKEN"));
});
