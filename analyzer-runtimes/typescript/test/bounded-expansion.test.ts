import assert from "node:assert/strict";
import test from "node:test";

import {
  appendBoundedExpansionResult,
  boundedWildcardSubstitution,
  createBoundedExpansionBudget,
  reserveBoundedTraversal,
} from "../dist/bounded-expansion.js";
import { packageExportTargetsForKey } from "../dist/package-info.js";

test("wildcard substitution rejects amplified output before constructing it", () => {
  const target = "*".repeat(4_096);
  const wildcard = "segment".repeat(4_096);
  const budget = createBoundedExpansionBudget({
    maxResultBytes: 64 * 1024,
    maxTotalBytes: 128 * 1024,
    maxResults: 8,
  });
  const originalReplaceAll = String.prototype.replaceAll;
  let amplifiedReplacementAttempted = false;
  try {
    String.prototype.replaceAll = function (
      searchValue: string | RegExp,
      replaceValue: string | ((substring: string, ...args: unknown[]) => string),
    ): string {
      if (String(this) === target) {
        amplifiedReplacementAttempted = true;
      }
      return Reflect.apply(originalReplaceAll, String(this), [
        searchValue,
        replaceValue,
      ]) as string;
    };

    assert.equal(
      boundedWildcardSubstitution(target, wildcard, budget),
      null,
    );
    assert.equal(amplifiedReplacementAttempted, false);
    assert.equal(budget.limited, true);
    assert.equal(budget.retainedResults, 0);
    assert.equal(budget.retainedBytes, 0);
  } finally {
    String.prototype.replaceAll = originalReplaceAll;
  }
});

test("expansion budgets enforce UTF-8, count, aggregate, and traversal limits", () => {
  const budget = createBoundedExpansionBudget({
    maxResults: 2,
    maxResultBytes: 4,
    maxTotalBytes: 6,
    maxTraversalEntries: 2,
  });
  const results: string[] = [];

  assert.equal(reserveBoundedTraversal(budget, 2), true);
  assert.equal(reserveBoundedTraversal(budget), false);
  assert.equal(appendBoundedExpansionResult(results, "é", budget), true);
  assert.equal(
    boundedWildcardSubstitution("a*", "界", budget),
    "a界",
  );
  assert.deepEqual(results, ["é"]);
  assert.equal(budget.retainedBytes, 6);
  assert.equal(
    boundedWildcardSubstitution("unused", "", budget),
    null,
  );
  assert.equal(budget.limited, true);

  const perResultBudget = createBoundedExpansionBudget({
    maxResultBytes: 2,
  });
  assert.equal(
    boundedWildcardSubstitution("abc", "", perResultBudget),
    null,
  );
  assert.equal(perResultBudget.retainedResults, 0);

  const aggregateBudget = createBoundedExpansionBudget({
    maxResultBytes: 4,
    maxTotalBytes: 3,
  });
  assert.equal(
    appendBoundedExpansionResult([], "é", aggregateBudget),
    true,
  );
  assert.equal(
    boundedWildcardSubstitution("a*", "x", aggregateBudget),
    null,
  );
  assert.equal(aggregateBudget.retainedBytes, 2);
});

test("package export traversal is iterative and deterministically bounded", () => {
  let deeplyNested: unknown = "./deep.ts";
  for (let depth = 0; depth < 10_000; depth += 1) {
    deeplyNested = [deeplyNested];
  }
  const deepBudget = createBoundedExpansionBudget();

  assert.deepEqual(
    packageExportTargetsForKey(deeplyNested, ".", deepBudget),
    [],
  );
  assert.equal(deepBudget.limited, true);

  const broadBudget = createBoundedExpansionBudget();
  const broadTargets = Array.from(
    { length: 5_000 },
    (_, index) => `./target-${index}.ts`,
  );
  const first = packageExportTargetsForKey(
    broadTargets,
    ".",
    broadBudget,
  );

  assert.equal(first.length, 512);
  assert.deepEqual(
    first.slice(0, 3),
    ["./target-0.ts", "./target-1.ts", "./target-2.ts"],
  );
  assert.equal(first.at(-1), "./target-511.ts");
  assert.equal(broadBudget.limited, true);
});

test("package export wildcard expansion shares the byte preflight", () => {
  const budget = createBoundedExpansionBudget();
  const wildcard = "x".repeat(8_192);
  const targets = packageExportTargetsForKey(
    {
      "./*": "*".repeat(8_192),
    },
    `./${wildcard}`,
    budget,
  );

  assert.deepEqual(targets, []);
  assert.equal(budget.limited, true);
  assert.equal(budget.retainedResults, 0);
});

test("package export limits charge only distinct targets", () => {
  const duplicateBudget = createBoundedExpansionBudget();
  const duplicateTargets = packageExportTargetsForKey(
    [
      ...Array.from({ length: 600 }, () => "./duplicate.ts"),
      "./distinct.ts",
    ],
    ".",
    duplicateBudget,
  );

  assert.deepEqual(
    duplicateTargets,
    ["./duplicate.ts", "./distinct.ts"],
  );
  assert.equal(duplicateBudget.retainedResults, 2);
  assert.equal(duplicateBudget.limited, false);

  const wildcardBudget = createBoundedExpansionBudget();
  const wildcardTargets = packageExportTargetsForKey(
    {
      "./*": [
        ...Array.from(
          { length: 600 },
          () => "./duplicate/*.ts",
        ),
        "./distinct/*.ts",
      ],
    },
    "./button",
    wildcardBudget,
  );

  assert.deepEqual(wildcardTargets, [
    "./duplicate/button.ts",
    "./distinct/button.ts",
  ]);
  assert.equal(wildcardBudget.retainedResults, 2);
  assert.equal(wildcardBudget.limited, false);

  const exactLimitBudget = createBoundedExpansionBudget();
  const exactTargets = Array.from(
    { length: 512 },
    (_, index) => `./target-${index}.ts`,
  );
  const retained = packageExportTargetsForKey(
    [...exactTargets, ...Array.from({ length: 100 }, () => exactTargets[0])],
    ".",
    exactLimitBudget,
  );

  assert.deepEqual(retained, exactTargets);
  assert.equal(exactLimitBudget.retainedResults, 512);
  assert.equal(exactLimitBudget.limited, false);
});
