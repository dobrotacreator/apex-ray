import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { analyze } from "../dist/analyzer.js";
import { parseArgs } from "../dist/cli.js";
import { findNearestConfig, normalizeTsConfigExtends } from "../dist/program.js";
import { loadRepoFileInventory } from "../dist/workspace/inventory.js";
import { writeFile } from "./helpers.js";

test("program helpers resolve nearest config and workspace package extends", () => {
  const repo = fs.mkdtempSync(path.join(os.tmpdir(), "apex-ray-ts-program-"));
  try {
    writeFile(
      repo,
      "packages/tsconfig/package.json",
      JSON.stringify({ name: "@workspace/tsconfig" }),
    );
    writeFile(
      repo,
      "packages/tsconfig/base.json",
      JSON.stringify({
        compilerOptions: {
          strict: true,
        },
      }),
    );
    writeFile(
      repo,
      "apps/web/tsconfig.json",
      JSON.stringify({
        extends: "@workspace/tsconfig/base.json",
        include: ["src/**/*.ts"],
      }),
    );
    writeFile(repo, "apps/web/src/cart.ts", "export const total = 1;\n");

    const configPath = findNearestConfig(repo, "apps/web/src/cart.ts");
    assert.equal(configPath, path.join(repo, "apps/web/tsconfig.json"));

    const normalized = normalizeTsConfigExtends(repo, configPath, {
      extends: "@workspace/tsconfig/base.json",
    });
    assert.deepEqual(normalized, {
      extends: path.join(repo, "packages/tsconfig/base.json"),
    });
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
  }
});

test("fallback inventory permits safe workspace package config targets", () => {
  const repo = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-program-fallback-package-config-"),
  );
  try {
    writeFile(
      repo,
      "packages/tsconfig/package.json",
      JSON.stringify({
        name: "@workspace/tsconfig",
        exports: {
          "./base": "./configs/base.config",
        },
      }),
    );
    writeFile(
      repo,
      "packages/tsconfig/configs/base.config",
      JSON.stringify({ compilerOptions: { strict: true } }),
    );
    writeFile(repo, "apps/web/tsconfig.json", "{}");
    writeFile(repo, "apps/web/src/cart.ts", "export const total = 1;\n");
    const args = parseArgs([
      "--repo",
      repo,
      "--changed",
      "apps/web/src/cart.ts",
      "--no-index-cache",
    ]);
    const inventory = loadRepoFileInventory(args);
    const configPath = path.join(repo, "apps/web/tsconfig.json");

    assert.equal(inventory.manifestBounded, false);
    assert.equal(
      inventory.configPathKeys.has(
        path.join(
          repo,
          "packages/tsconfig/configs/base.config",
        ),
      ),
      false,
    );
    assert.deepEqual(
      normalizeTsConfigExtends(
        repo,
        configPath,
        { extends: "@workspace/tsconfig/base" },
        inventory,
      ),
      {
        extends: path.join(
          repo,
          "packages/tsconfig/configs/base.config",
        ),
      },
    );
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
  }
});

test("workspace package subpath exports resolve tsconfig extends", () => {
  const repo = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-program-package-exports-"),
  );
  try {
    writeFile(
      repo,
      "packages/tsconfig/package.json",
      JSON.stringify({
        name: "@workspace/tsconfig",
        exports: {
          "./base": {
            types: "./configs/base.json",
            default: "./dist/base.js",
          },
        },
      }),
    );
    writeFile(
      repo,
      "packages/tsconfig/configs/base.json",
      JSON.stringify({ compilerOptions: { strict: true } }),
    );
    writeFile(repo, "apps/web/tsconfig.json", "{}");
    writeFile(repo, "apps/web/src/cart.ts", "export const total = 1;\n");
    const manifestPath = path.join(repo, "files.json");
    fs.writeFileSync(
      manifestPath,
      JSON.stringify({
        version: 2,
        files: ["apps/web/src/cart.ts"],
        package_files: ["packages/tsconfig/package.json"],
        config_files: [
          "apps/web/tsconfig.json",
          "packages/tsconfig/configs/base.json",
        ],
      }),
      "utf8",
    );
    const args = parseArgs([
      "--repo",
      repo,
      "--changed",
      "apps/web/src/cart.ts",
      "--file-manifest",
      manifestPath,
      "--no-index-cache",
    ]);
    const inventory = loadRepoFileInventory(args);
    const configPath = path.join(repo, "apps/web/tsconfig.json");

    assert.deepEqual(
      normalizeTsConfigExtends(
        repo,
        configPath,
        { extends: "@workspace/tsconfig/base" },
        inventory,
      ),
      {
        extends: path.join(
          repo,
          "packages/tsconfig/configs/base.json",
        ),
      },
    );
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
  }
});

test("workspace package root exports and tsconfig field resolve bare extends", () => {
  const repo = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-program-package-root-config-"),
  );
  try {
    const packages = [
      {
        directory: "conditional",
        manifest: {
          name: "@workspace/conditional",
          exports: {
            ".": {
              types: "./configs/root.json",
              default: "./dist/index.js",
            },
          },
        },
        config: "configs/root.json",
      },
      {
        directory: "string",
        manifest: {
          name: "@workspace/string",
          exports: "./configs/root.json",
        },
        config: "configs/root.json",
      },
      {
        directory: "field",
        manifest: {
          name: "@workspace/field",
          exports: "./dist/index.js",
          tsconfig: "./configs/root.json",
        },
        config: "configs/root.json",
      },
    ];
    for (const packageFixture of packages) {
      writeFile(
        repo,
        `packages/${packageFixture.directory}/package.json`,
        JSON.stringify(packageFixture.manifest),
      );
      writeFile(
        repo,
        `packages/${packageFixture.directory}/${packageFixture.config}`,
        JSON.stringify({ compilerOptions: { strict: true } }),
      );
    }
    writeFile(repo, "apps/web/tsconfig.json", "{}");
    writeFile(repo, "apps/web/src/cart.ts", "export const total = 1;\n");
    const manifestPath = path.join(repo, "files.json");
    fs.writeFileSync(
      manifestPath,
      JSON.stringify({
        version: 2,
        files: ["apps/web/src/cart.ts"],
        package_files: packages.map(
          (packageFixture) =>
            `packages/${packageFixture.directory}/package.json`,
        ),
        config_files: [
          "apps/web/tsconfig.json",
          ...packages.map(
            (packageFixture) =>
              `packages/${packageFixture.directory}/${packageFixture.config}`,
          ),
        ],
      }),
      "utf8",
    );
    const args = parseArgs([
      "--repo",
      repo,
      "--changed",
      "apps/web/src/cart.ts",
      "--file-manifest",
      manifestPath,
      "--no-index-cache",
    ]);
    const inventory = loadRepoFileInventory(args);
    const configPath = path.join(repo, "apps/web/tsconfig.json");

    for (const packageFixture of packages) {
      assert.deepEqual(
        normalizeTsConfigExtends(
          repo,
          configPath,
          { extends: packageFixture.manifest.name },
          inventory,
        ),
        {
          extends: path.join(
            repo,
            `packages/${packageFixture.directory}/${packageFixture.config}`,
          ),
        },
      );
    }
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
  }
});

test("workspace package extends resolve transitively for manifest-backed programs", () => {
  const repo = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-program-transitive-package-config-"),
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
      `{
        // Keep this JSONC-only syntax covered while normalizing the config.
        "compilerOptions": {
          "baseUrl": "../..",
          "moduleResolution": "Bundler",
          "paths": {
            "@shared/value": ["packages/shared/value.ts"],
          },
          "target": "ES2022",
        },
      }`,
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
        "extends": "@workspace/base/base.json",
      }`,
    );
    writeFile(
      repo,
      "packages/shared/value.ts",
      'export function sharedValue(): "TRANSITIVE_CONFIG_LITERAL" { return "TRANSITIVE_CONFIG_LITERAL"; }\n',
    );
    writeFile(
      repo,
      "apps/web/tsconfig.json",
      JSON.stringify({
        extends: "@workspace/mid/mid.json",
        include: ["src/**/*"],
      }),
    );
    writeFile(
      repo,
      "apps/web/src/changed.ts",
      [
        "import { sharedValue } from '@shared/value';",
        "export function visible() { return sharedValue(); }",
      ].join("\n"),
    );
    const manifestPath = path.join(repo, "files.json");
    fs.writeFileSync(
      manifestPath,
      JSON.stringify({
        version: 2,
        files: [
          "apps/web/src/changed.ts",
          "packages/shared/value.ts",
        ],
        package_files: [
          "packages/base/package.json",
          "packages/mid/package.json",
        ],
        config_files: [
          "apps/web/tsconfig.json",
          "packages/base/base.json",
          "packages/mid/mid.json",
        ],
      }),
      "utf8",
    );
    const args = parseArgs([
      "--repo",
      repo,
      "--changed",
      "apps/web/src/changed.ts",
      "--range",
      "apps/web/src/changed.ts:1-2",
      "--file-manifest",
      manifestPath,
      "--no-index-cache",
    ]);

    const result = analyze(args);

    const visible = result.files[0]?.symbols.find(
      (symbol) => symbol.name === "visible",
    );
    assert.equal(visible?.signature, '(): "TRANSITIVE_CONFIG_LITERAL"');
    assert.equal(result.partial, false);
    assert.equal(
      result.warnings.some((warning) =>
        warning.includes("@workspace/base/base.json")
      ),
      false,
    );
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
  }
});

test("nested workspace package config cycles and omitted targets stay bounded", () => {
  const repo = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-program-nested-package-safety-"),
  );
  try {
    for (const packageName of ["a", "b", "omitted"]) {
      writeFile(
        repo,
        `packages/${packageName}/package.json`,
        JSON.stringify({ name: `@workspace/${packageName}` }),
      );
    }
    writeFile(
      repo,
      "packages/a/base.json",
      JSON.stringify({ extends: "@workspace/b/base.json" }),
    );
    writeFile(
      repo,
      "packages/b/base.json",
      JSON.stringify({ extends: "@workspace/a/base.json" }),
    );
    writeFile(
      repo,
      "packages/omitted/base.json",
      JSON.stringify({ compilerOptions: { strict: true } }),
    );
    writeFile(
      repo,
      "apps/web/tsconfig.json",
      JSON.stringify({ extends: "@workspace/a/base.json" }),
    );
    writeFile(repo, "apps/web/src/changed.ts", "export const changed = 1;\n");
    const manifestPath = path.join(repo, "files.json");
    fs.writeFileSync(
      manifestPath,
      JSON.stringify({
        version: 2,
        files: ["apps/web/src/changed.ts"],
        package_files: [
          "packages/a/package.json",
          "packages/b/package.json",
          "packages/omitted/package.json",
        ],
        config_files: [
          "apps/web/tsconfig.json",
          "packages/a/base.json",
          "packages/b/base.json",
        ],
      }),
      "utf8",
    );
    const analyzerArgs = () =>
      parseArgs([
        "--repo",
        repo,
        "--changed",
        "apps/web/src/changed.ts",
        "--file-manifest",
        manifestPath,
        "--no-index-cache",
      ]);

    const cyclicResult = analyze(analyzerArgs());

    assert.equal(cyclicResult.files.length, 1);
    assert.equal(cyclicResult.partial, true);
    assert.ok(
      cyclicResult.warnings.some((warning) =>
        warning.includes("Circularity detected")
      ),
    );

    writeFile(
      repo,
      "packages/b/base.json",
      JSON.stringify({ extends: "@workspace/omitted/base.json" }),
    );
    const omittedResult = analyze(analyzerArgs());

    assert.equal(omittedResult.files.length, 1);
    assert.equal(omittedResult.partial, true);
    assert.ok(
      omittedResult.warnings.some((warning) =>
        warning.includes("@workspace/omitted/base.json")
      ),
    );
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
  }
});

test("workspace package config targets must be safe and present in the inventory", () => {
  const repo = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-program-package-safety-"),
  );
  try {
    writeFile(
      repo,
      "packages/omitted/package.json",
      JSON.stringify({
        name: "@workspace/omitted",
        exports: {
          "./base": "./configs/base.json",
        },
      }),
    );
    writeFile(
      repo,
      "packages/omitted/configs/base.json",
      JSON.stringify({ compilerOptions: { strict: true } }),
    );
    writeFile(
      repo,
      "packages/unsafe/package.json",
      JSON.stringify({
        name: "@workspace/unsafe",
        exports: {
          "./base": "./../../../outside.json",
        },
      }),
    );
    writeFile(
      repo,
      "packages/legacy/package.json",
      JSON.stringify({ name: "@workspace/legacy" }),
    );
    writeFile(
      repo,
      "packages/legacy/base.json",
      JSON.stringify({ compilerOptions: { strict: true } }),
    );
    writeFile(
      repo,
      "packages/empty/package.json",
      JSON.stringify({ name: "@workspace/empty" }),
    );
    writeFile(
      repo,
      "apps/web/tsconfig.json",
      JSON.stringify({
        extends: "@workspace/omitted/base",
        files: ["src/cart.ts"],
      }),
    );
    writeFile(repo, "apps/web/src/cart.ts", "export const total = 1;\n");
    const manifestPath = path.join(repo, "files.json");
    fs.writeFileSync(
      manifestPath,
      JSON.stringify({
        version: 2,
        files: ["apps/web/src/cart.ts"],
        package_files: [
          "packages/omitted/package.json",
          "packages/unsafe/package.json",
          "packages/legacy/package.json",
          "packages/empty/package.json",
        ],
        config_files: ["apps/web/tsconfig.json"],
      }),
      "utf8",
    );
    const args = parseArgs([
      "--repo",
      repo,
      "--changed",
      "apps/web/src/cart.ts",
      "--range",
      "apps/web/src/cart.ts:1-1",
      "--file-manifest",
      manifestPath,
      "--no-index-cache",
    ]);
    const inventory = loadRepoFileInventory(args);
    const configPath = path.join(repo, "apps/web/tsconfig.json");

    for (const specifier of [
      "@workspace/omitted/base",
      "@workspace/unsafe/base",
      "@workspace/legacy/base.json",
      "@workspace/empty",
    ]) {
      assert.deepEqual(
        normalizeTsConfigExtends(
          repo,
          configPath,
          { extends: specifier },
          inventory,
        ),
        { extends: specifier },
      );
    }

    const result = analyze(args);
    assert.equal(result.files.length, 1);
    assert.equal(result.partial, true);
    assert.ok(
      result.warnings.some((warning) =>
        warning.includes("configuration could not be read completely")
      ),
    );
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
  }
});

test("workspace package resolution caches are isolated by manifest inventory", () => {
  const repo = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-program-inventory-cache-"),
  );
  const originalOpenSync = fs.openSync;
  try {
    for (const packageName of ["alpha", "bravo"]) {
      writeFile(
        repo,
        `packages/${packageName}/package.json`,
        JSON.stringify({ name: `@workspace/${packageName}` }),
      );
      writeFile(
        repo,
        `packages/${packageName}/base.json`,
        JSON.stringify({ compilerOptions: { strict: true } }),
      );
    }
    writeFile(repo, "apps/web/tsconfig.json", "{}");
    writeFile(repo, "apps/web/src/cart.ts", "export const total = 1;\n");
    const broadManifestPath = path.join(repo, "broad-files.json");
    const narrowManifestPath = path.join(repo, "narrow-files.json");
    fs.writeFileSync(
      broadManifestPath,
      JSON.stringify({
        version: 2,
        files: ["apps/web/src/cart.ts"],
        package_files: [
          "packages/alpha/package.json",
          "packages/bravo/package.json",
        ],
        config_files: [
          "packages/alpha/base.json",
          "packages/bravo/base.json",
        ],
      }),
      "utf8",
    );
    fs.writeFileSync(
      narrowManifestPath,
      JSON.stringify({
        version: 2,
        files: ["apps/web/src/cart.ts"],
      }),
      "utf8",
    );
    const argsForManifest = (manifestPath: string) =>
      parseArgs([
        "--repo",
        repo,
        "--changed",
        "apps/web/src/cart.ts",
        "--file-manifest",
        manifestPath,
        "--no-index-cache",
      ]);
    const broadInventory = loadRepoFileInventory(
      argsForManifest(broadManifestPath),
    );
    const narrowInventory = loadRepoFileInventory(
      argsForManifest(narrowManifestPath),
    );
    const configPath = path.join(repo, "apps", "web", "tsconfig.json");
    const packagePaths = new Set(
      ["alpha", "bravo"].map((packageName) =>
        path.join(repo, "packages", packageName, "package.json")
      ),
    );
    let packageOpenCount = 0;
    fs.openSync = ((candidate: fs.PathLike, ...rest: unknown[]) => {
      if (packagePaths.has(path.resolve(String(candidate)))) {
        packageOpenCount += 1;
      }
      return (originalOpenSync as (...args: unknown[]) => number)(
        candidate,
        ...rest,
      );
    }) as typeof fs.openSync;

    const broadAlpha = normalizeTsConfigExtends(
      repo,
      configPath,
      { extends: "@workspace/alpha/base.json" },
      broadInventory,
    );
    const narrowAlpha = normalizeTsConfigExtends(
      repo,
      configPath,
      { extends: "@workspace/alpha/base.json" },
      narrowInventory,
    );
    const narrowBravo = normalizeTsConfigExtends(
      repo,
      configPath,
      { extends: "@workspace/bravo/base.json" },
      narrowInventory,
    );
    const broadBravo = normalizeTsConfigExtends(
      repo,
      configPath,
      { extends: "@workspace/bravo/base.json" },
      broadInventory,
    );

    assert.deepEqual(broadAlpha, {
      extends: path.join(repo, "packages/alpha/base.json"),
    });
    assert.deepEqual(narrowAlpha, {
      extends: "@workspace/alpha/base.json",
    });
    assert.deepEqual(narrowBravo, {
      extends: "@workspace/bravo/base.json",
    });
    assert.deepEqual(broadBravo, {
      extends: path.join(repo, "packages/bravo/base.json"),
    });
    assert.equal(packageOpenCount, 2);
  } finally {
    fs.openSync = originalOpenSync;
    fs.rmSync(repo, { recursive: true, force: true });
  }
});
