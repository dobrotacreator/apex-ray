import assert from "node:assert/strict";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import ts from "typescript";

import {
  collectClassHeritageIndex,
  collectIdentifierIndex,
  collectReceiverIndex,
  collectTypeAliasIndex,
} from "../dist/semantic-file-index.js";

test("semantic file index captures identifiers, receivers, aliases, and heritage", () => {
  const repo = path.join(os.tmpdir(), "apex-ray-ts-semantic-file-index");
  const absPath = path.join(repo, "src/cart.ts");
  const text = [
    "interface BasePort {}",
    "interface ChildPort extends BasePort {}",
    "class BaseService {}",
    "class Repo {}",
    "class ImplService { run(input: CartDto): CartResult { return input as CartResult; } }",
    "class CartService extends BaseService implements ChildPort {",
    "  constructor(private repo: Repo) {}",
    "  service = new ImplService();",
    "  handle(input: CartDto): CartResult {",
    "    this.service['run'] = this.service.run;",
    "    const local = new ImplService();",
    "    return local.run(input);",
    "  }",
    "}",
    "type CartAlias = CartService;",
  ].join("\n");
  const source = ts.createSourceFile(absPath, text, ts.ScriptTarget.ES2022, true, ts.ScriptKind.TS);

  const identifiers = collectIdentifierIndex(repo, source);
  const receivers = collectReceiverIndex(repo, source);
  const aliases = collectTypeAliasIndex(source);
  const heritages = collectClassHeritageIndex(source);

  assert.ok(identifiers.some((item) => item.name === "run" && item.namespaceQualifier === "this.service" && item.reference.kind === "write"));
  assert.ok(identifiers.some((item) => item.name === "run" && item.namespaceQualifier === "this.service" && item.reference.kind === "read"));
  assert.ok(identifiers.some((item) => item.name === "run" && item.namespaceQualifier === "local"));

  assert.ok(receivers.some((item) => item.receiverName === "this" && item.typeName === "CartService"));
  assert.ok(receivers.some((item) => item.receiverName === "super" && item.typeName === "BaseService"));
  assert.ok(receivers.some((item) => item.receiverName === "this.repo" && item.typeName === "Repo"));
  assert.ok(receivers.some((item) => item.receiverName === "this.service" && item.typeName === "ImplService"));
  assert.ok(receivers.some((item) => item.receiverName === "local" && item.typeName === "ImplService"));
  assert.ok(receivers.some((item) => item.receiverName === "input" && item.typeName === "CartDto"));

  assert.ok(aliases.some((item) => item.name === "CartAlias" && item.targetName === "CartService"));
  assert.ok(heritages.some((item) => item.className === "ChildPort" && item.baseNames.includes("BasePort")));
  assert.ok(heritages.some((item) => item.className === "CartService" && item.baseNames.includes("BaseService")));
  assert.ok(heritages.some((item) => item.className === "CartService" && item.baseNames.includes("ChildPort")));
});
