import assert from "node:assert/strict";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { indexSourceFile, isAnalyzableSourceFile } from "../dist/indexes/source-file-index.js";

test("source file index captures imports, exports, receivers, heritage, aliases, and DI", () => {
  const repo = path.join(os.tmpdir(), "apex-ray-ts-source-file-index");
  const relPath = "src/module.ts";
  const absPath = path.join(repo, relPath);
  const text = [
    "import DefaultThing, { Named as LocalNamed } from './dep.js';",
    "import * as Namespace from './namespace.js';",
    "const { readFile: readFileLocal } = require('node:fs');",
    "const dynamic = await import('./lazy.js');",
    "export { LocalNamed as ExportedNamed } from './dep.js';",
    "module.exports.extra = LocalNamed;",
    "interface BasePort {}",
    "export interface ChildPort extends BasePort {}",
    "class BaseService {}",
    "class Repo {}",
    "class ImplService { run(): void {} }",
    "const CART_TOKEN = Symbol('cart');",
    "class CartService extends BaseService implements ChildPort {",
    "  constructor(private repo: Repo) {}",
    "  service = new ImplService();",
    "  handle(input: CartDto): CartResult {",
    "    return this.service.run(input);",
    "  }",
    "}",
    "type CartAlias = CartService;",
    "const PROVIDERS = [{ provide: CART_TOKEN, useClass: CartService, inject: [Repo] }];",
    "@Module({ providers: [...PROVIDERS], exports: [CartService] })",
    "class CartModule {}",
    "function handler(@Inject(CART_TOKEN) service: CartService): void {}",
  ].join("\n");

  const entry = indexSourceFile({
    repo,
    absPath,
    relPath,
    size: Buffer.byteLength(text),
    mtimeMs: 123,
    text,
  });

  assert.equal(isAnalyzableSourceFile(absPath), true);
  assert.equal(isAnalyzableSourceFile(path.join(repo, "src/types.d.ts")), false);
  assert.equal(entry.relPath, relPath);
  assert.equal(entry.relLower, relPath);
  assert.equal(entry.size, Buffer.byteLength(text));
  assert.equal(entry.mtimeMs, 123);

  assert.ok(entry.imports.some((item) => item.moduleSpecifier === "./dep.js" && item.defaultImport?.localName === "DefaultThing"));
  assert.ok(entry.imports.some((item) => item.moduleSpecifier === "./namespace.js" && item.namespaceImport?.localName === "Namespace"));
  assert.ok(entry.imports.some((item) => item.moduleSpecifier === "node:fs" && item.namedImports[0]?.localName === "readFileLocal"));
  assert.ok(entry.imports.some((item) => item.moduleSpecifier === "./lazy.js" && item.namespaceImport?.localName === "dynamic"));
  assert.ok(entry.exports.some((item) => item.moduleSpecifier === "./dep.js" && item.exportedName === "ExportedNamed"));
  assert.ok(entry.exports.some((item) => item.moduleSpecifier === null && item.exportedName === "extra"));

  assert.ok(entry.identifiers.some((item) => item.name === "run" && item.namespaceQualifier === "this.service"));
  assert.ok(entry.receivers.some((item) => item.receiverName === "this" && item.typeName === "CartService"));
  assert.ok(entry.receivers.some((item) => item.receiverName === "super" && item.typeName === "BaseService"));
  assert.ok(entry.receivers.some((item) => item.receiverName === "this.repo" && item.typeName === "Repo"));
  assert.ok(entry.receivers.some((item) => item.receiverName === "this.service" && item.typeName === "ImplService"));

  assert.ok(entry.typeAliases.some((item) => item.name === "CartAlias" && item.targetName === "CartService"));
  assert.ok(entry.classHeritages.some((item) => item.className === "ChildPort" && item.baseNames.includes("BasePort")));
  assert.ok(entry.classHeritages.some((item) => item.className === "CartService" && item.baseNames.includes("BaseService")));
  assert.ok(entry.classHeritages.some((item) => item.className === "CartService" && item.baseNames.includes("ChildPort")));
  assert.ok(
    entry.diProviders.some(
      (item) => item.tokenName === "CART_TOKEN" && item.implementationName === "CartService" && item.sourceArrayName === "PROVIDERS",
    ),
  );
  assert.ok(entry.diProviders.some((item) => item.tokenName === "CART_TOKEN" && item.implementationName === "Repo"));
  assert.ok(entry.diInjections.some((item) => item.tokenName === "CART_TOKEN"));
});
