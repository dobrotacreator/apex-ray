import assert from "node:assert/strict";
import test from "node:test";

import { addReference, mergeReferences } from "../dist/references/reference-merge.js";
import type { Reference } from "../dist/types.js";

test("reference merge prioritizes high signal refs and deduplicates", () => {
  const duplicateCall: Reference = {
    file: "src/cart.ts",
    line: 10,
    text: "calculateTotal(items);",
    kind: "call",
  };
  const refs: Reference[] = [
    { file: "src/cart.test.ts", line: 20, text: "calculateTotal(items);", kind: "call" },
    { file: "src/cart.ts", line: 3, text: "import { calculateTotal } from './cart.js';", kind: "import" },
    duplicateCall,
    { ...duplicateCall },
    { file: "src/cart.ts", line: 12, text: "total = calculateTotal(items);", kind: "write" },
  ];

  assert.deepEqual(mergeReferences(refs, 3), [
    duplicateCall,
    { file: "src/cart.ts", line: 12, text: "total = calculateTotal(items);", kind: "write" },
    { file: "src/cart.test.ts", line: 20, text: "calculateTotal(items);", kind: "call" },
  ]);

  const seen = new Set<string>();
  const added: Reference[] = [];
  addReference(added, seen, duplicateCall, 1);
  addReference(added, seen, { ...duplicateCall }, 1);
  addReference(added, seen, { file: "src/cart.ts", line: 11, text: "other();", kind: "call" }, 1);
  assert.deepEqual(added, [duplicateCall]);
});
