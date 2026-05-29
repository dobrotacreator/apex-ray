import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { parseArgs } from "../dist/cli.js";
import { buildRepoIndex } from "../dist/indexes/repo-index.js";
import { findRelatedTests, isTestPath } from "../dist/test-discovery.js";
import type { Reference } from "../dist/types.js";
import { writeFile } from "./helpers.js";

test("test discovery finds related runnable tests", () => {
  const repo = fs.mkdtempSync(path.join(os.tmpdir(), "apex-ray-ts-test-discovery-"));
  try {
    writeFile(
      repo,
      "vitest.config.ts",
      [
        "export default {",
        "  test: {",
        "    include: ['src/**/*.test.ts', 'tests/**/*.spec.ts'],",
        "    exclude: ['tests/excluded.spec.ts'],",
        "  },",
        "};",
      ].join("\n"),
    );
    writeFile(repo, "src/cart.ts", "export class CartService {}\n");
    writeFile(repo, "src/cart.test.ts", "import { CartService } from './cart.js';\nnew CartService();\n");
    writeFile(
      repo,
      "src/checkout.ts",
      "import { CartService } from './cart.js';\nexport function checkout() { return new CartService(); }\n",
    );
    writeFile(repo, "tests/checkout.spec.ts", "import { checkout } from '../src/checkout.js';\ncheckout();\n");
    writeFile(repo, "tests/excluded.spec.ts", "import { checkout } from '../src/checkout.js';\ncheckout();\n");
    writeFile(repo, "e2e/cart.spec.ts", "import { CartService } from '../src/cart.js';\nnew CartService();\n");

    const repoIndex = buildRepoIndex(parseArgs(["--repo", repo, "--changed", "src/cart.ts", "--no-index-cache"]));
    const references: Reference[] = [
      { file: "src/checkout.ts", line: 2, text: "export function checkout() { return new CartService(); }", kind: "call" },
      { file: "tests/checkout.spec.ts", line: 2, text: "checkout();", kind: "call" },
      { file: "tests/excluded.spec.ts", line: 2, text: "checkout();", kind: "call" },
    ];

    const related = findRelatedTests(repo, repoIndex, "src/cart.ts", references);

    assert.equal(isTestPath("src/cart.test.ts"), true);
    assert.equal(isTestPath("src/contest.ts"), false);
    assert.equal(related[0], "src/cart.test.ts");
    assert.ok(related.includes("tests/checkout.spec.ts"));
    assert.equal(related.includes("tests/excluded.spec.ts"), false);
    assert.equal(related.includes("e2e/cart.spec.ts"), false);
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
  }
});
