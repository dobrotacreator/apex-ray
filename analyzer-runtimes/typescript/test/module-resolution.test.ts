import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { parseArgs } from "../dist/cli.js";
import {
  findIndexedPackageForFile,
  isModuleSpecifierRelatedToPath,
  moduleSpecifierCandidatePaths,
} from "../dist/module-resolution.js";
import { buildRepoIndex } from "../dist/indexes/repo.js";
import type { PackageInfo } from "../dist/types.js";
import { assertIncludesPath, writeFile } from "./helpers.js";

test("module resolution expands relative imports and tsconfig path aliases", () => {
  const repo = fs.mkdtempSync(path.join(os.tmpdir(), "apex-ray-ts-module-resolution-"));
  try {
    writeFile(
      repo,
      "tsconfig.json",
      JSON.stringify({
        compilerOptions: {
          baseUrl: ".",
          paths: {
            "@app/*": ["src/*"],
            "@exact": ["src/exact.ts"],
          },
        },
        include: ["src/**/*.ts"],
      }),
    );
    writeFile(repo, "src/features/cart.ts", "export const cart = 1;\n");
    writeFile(repo, "src/shared.ts", "export const shared = 1;\n");
    writeFile(repo, "src/exact.ts", "export const exact = 1;\n");

    const importerPath = path.join(repo, "src/features/cart.ts");

    assertIncludesPath(
      moduleSpecifierCandidatePaths("../shared.js", importerPath, repo, null),
      path.join(repo, "src/shared.ts"),
    );
    assertIncludesPath(
      moduleSpecifierCandidatePaths("../modern.mjs", importerPath, repo, null),
      path.join(repo, "src/modern.mts"),
    );
    assertIncludesPath(
      moduleSpecifierCandidatePaths("../legacy.cjs", importerPath, repo, null),
      path.join(repo, "src/legacy.cts"),
    );
    assertIncludesPath(
      moduleSpecifierCandidatePaths("../browser", importerPath, repo, null),
      path.join(repo, "src/browser.mjs"),
    );
    assertIncludesPath(
      moduleSpecifierCandidatePaths("../worker", importerPath, repo, null),
      path.join(repo, "src/worker.cjs"),
    );
    assert.equal(
      moduleSpecifierCandidatePaths("../shared.js", importerPath, repo, null).includes(
        path.join(repo, "src/shared.mts"),
      ),
      false,
    );
    assert.equal(
      moduleSpecifierCandidatePaths("../modern.mjs", importerPath, repo, null).includes(
        path.join(repo, "src/modern.cts"),
      ),
      false,
    );
    assert.equal(
      moduleSpecifierCandidatePaths("../legacy.cjs", importerPath, repo, null).includes(
        path.join(repo, "src/legacy.ts"),
      ),
      false,
    );
    assertIncludesPath(
      moduleSpecifierCandidatePaths("@app/shared", importerPath, repo, null),
      path.join(repo, "src/shared.ts"),
    );
    assertIncludesPath(
      moduleSpecifierCandidatePaths("@exact", importerPath, repo, null),
      path.join(repo, "src/exact.ts"),
    );
    assert.equal(
      isModuleSpecifierRelatedToPath("../shared.js", importerPath, path.join(repo, "src/shared.ts"), null),
      true,
    );
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
  }
});

test("path alias resolution follows transitive workspace package configs", () => {
  const repo = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-module-transitive-config-"),
  );
  try {
    writeFile(
      repo,
      "packages/base/package.json",
      JSON.stringify({ name: "@workspace/base" }),
    );
    writeFile(
      repo,
      "packages/base/base.json",
      JSON.stringify({
        compilerOptions: {
          baseUrl: "../..",
          paths: {
            "@shared/value": ["packages/shared/value.ts"],
          },
        },
      }),
    );
    writeFile(
      repo,
      "packages/mid/package.json",
      JSON.stringify({ name: "@workspace/mid" }),
    );
    writeFile(
      repo,
      "packages/mid/mid.json",
      `{
        // Nested package-style extends must be normalized as JSONC.
        "extends": "@workspace/base/base.json",
      }`,
    );
    writeFile(repo, "packages/shared/value.ts", "export const value = 1;\n");
    writeFile(
      repo,
      "apps/web/tsconfig.json",
      JSON.stringify({ extends: "@workspace/mid/mid.json" }),
    );
    writeFile(repo, "apps/web/src/changed.ts", "export const changed = 1;\n");
    const importerPath = path.join(repo, "apps/web/src/changed.ts");

    assertIncludesPath(
      moduleSpecifierCandidatePaths(
        "@shared/value",
        importerPath,
        repo,
        null,
      ),
      path.join(repo, "packages/shared/value.ts"),
    );
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
  }
});

test("path alias resolution ignores external and symlinked extended configs", () => {
  const repo = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-module-config-boundary-"),
  );
  const outside = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-module-config-outside-"),
  );
  const originalOpenSync = fs.openSync;
  const forbiddenOpenPaths = new Set<string>();
  let forbiddenOpenCount = 0;
  try {
    const externalConfigPath = path.join(outside, "external.json");
    fs.writeFileSync(
      externalConfigPath,
      JSON.stringify({
        compilerOptions: {
          baseUrl: ".",
          paths: {
            "@external/*": ["private/*"],
            "@symlink/*": ["symlink-private/*"],
          },
        },
      }),
      "utf8",
    );
    forbiddenOpenPaths.add(externalConfigPath);
    fs.openSync = ((candidate: fs.PathLike, ...rest: unknown[]) => {
      if (forbiddenOpenPaths.has(path.resolve(String(candidate)))) {
        forbiddenOpenCount += 1;
      }
      return (originalOpenSync as (...args: unknown[]) => number)(
        candidate,
        ...rest,
      );
    }) as typeof fs.openSync;
    writeFile(repo, "src/use.ts", "export const use = 1;\n");
    const importerPath = path.join(repo, "src/use.ts");

    writeFile(
      repo,
      "tsconfig.json",
      JSON.stringify({
        extends: path.relative(repo, externalConfigPath),
      }),
    );
    assert.equal(
      moduleSpecifierCandidatePaths(
        "@external/secret",
        importerPath,
        repo,
        null,
      ).length,
      0,
    );

    const symlinkPath = path.join(repo, "config", "base.json");
    fs.mkdirSync(path.dirname(symlinkPath), { recursive: true });
    fs.symlinkSync(externalConfigPath, symlinkPath);
    forbiddenOpenPaths.add(symlinkPath);
    writeFile(
      repo,
      "nested/tsconfig.json",
      JSON.stringify({
        extends: "../config/base.json",
      }),
    );
    writeFile(repo, "nested/use.ts", "export const nested = 1;\n");
    assert.equal(
      moduleSpecifierCandidatePaths(
        "@symlink/secret",
        path.join(repo, "nested/use.ts"),
        repo,
        null,
      ).length,
      0,
    );
    assert.equal(forbiddenOpenCount, 0);
  } finally {
    fs.openSync = originalOpenSync;
    fs.rmSync(repo, { recursive: true, force: true });
    fs.rmSync(outside, { recursive: true, force: true });
  }
});

test("module resolution expands workspace package root, subpath, and wildcard exports", () => {
  const repo = fs.mkdtempSync(path.join(os.tmpdir(), "apex-ray-ts-package-resolution-"));
  try {
    const packageRoot = path.join(repo, "packages/lib");
    writeFile(
      repo,
      "packages/lib/package.json",
      JSON.stringify({
        name: "@acme/lib",
        exports: {
          ".": {
            types: "./src/index.ts",
          },
          "./feature": {
            import: "./src/feature.ts",
          },
          "./wild/*": "./src/wild/*.ts",
        },
      }),
    );
    writeFile(repo, "packages/lib/src/index.ts", "export const root = 1;\n");
    writeFile(repo, "packages/lib/src/feature.ts", "export const feature = 1;\n");
    writeFile(repo, "packages/lib/src/wild/button.ts", "export const button = 1;\n");
    writeFile(repo, "apps/web/src/use.ts", "export const use = 1;\n");

    const packageInfo: PackageInfo = {
      root: packageRoot,
      name: "@acme/lib",
      exports: {
        ".": {
          types: "./src/index.ts",
        },
        "./feature": {
          import: "./src/feature.ts",
        },
        "./wild/*": "./src/wild/*.ts",
      },
      main: null,
      module: null,
      tsconfig: null,
      types: null,
      typings: null,
    };
    const importerPath = path.join(repo, "apps/web/src/use.ts");

    assertIncludesPath(
      moduleSpecifierCandidatePaths("@acme/lib", importerPath, repo, packageInfo),
      path.join(packageRoot, "src/index.ts"),
    );
    assertIncludesPath(
      moduleSpecifierCandidatePaths(
        "@acme/lib",
        importerPath,
        repo,
        {
          ...packageInfo,
          exports: {
            types: "./configs/root.ts",
            default: "./dist/index.js",
          },
        },
      ),
      path.join(packageRoot, "configs/root.ts"),
    );
    assertIncludesPath(
      moduleSpecifierCandidatePaths("@acme/lib/feature", importerPath, repo, packageInfo),
      path.join(packageRoot, "src/feature.ts"),
    );
    assertIncludesPath(
      moduleSpecifierCandidatePaths("@acme/lib/wild/button", importerPath, repo, packageInfo),
      path.join(packageRoot, "src/wild/button.ts"),
    );
    assert.equal(
      isModuleSpecifierRelatedToPath(
        "@acme/lib/feature",
        importerPath,
        path.join(packageRoot, "src/feature.ts"),
        packageInfo,
      ),
      true,
    );
    assert.equal(
      isModuleSpecifierRelatedToPath(
        "@acme/lib/feature",
        importerPath,
        path.join(packageRoot, "src/other.ts"),
        packageInfo,
      ),
      false,
    );

    const repoIndex = buildRepoIndex(
      parseArgs(["--repo", repo, "--changed", "packages/lib/src/index.ts", "--no-index-cache"]),
    );
    const indexedPackage = findIndexedPackageForFile(repo, repoIndex, path.join(packageRoot, "src/index.ts"));
    assert.equal(indexedPackage?.name, "@acme/lib");
    assert.equal(
      findIndexedPackageForFile(repo, repoIndex, path.join(packageRoot, "src/index.ts")),
      indexedPackage,
    );
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
  }
});
