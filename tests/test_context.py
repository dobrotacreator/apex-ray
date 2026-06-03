from pathlib import Path

from apex_ray.analyzers import run_python_analyzer, run_typescript_analyzer
from apex_ray.classify import classify_diff
from apex_ray.context import _estimated_pack_chars, _finalize_pack, _risk_signals_for_symbols, build_context_packs
from apex_ray.diff import parse_unified_diff
from apex_ray.llm import review_cache_key
from apex_ray.models import (
    AnalyzerConfig,
    AnalyzerFile,
    AnalyzerReference,
    AnalyzerResult,
    AnalyzerSymbol,
    ChangedFile,
    CodeSnippet,
    ContextConfig,
    ContextPack,
    FileKind,
    LLMConfig,
    LLMProviderName,
    MemoryCard,
    ReviewConfig,
    ReviewRule,
    RiskSeverity,
    RiskSignal,
    TargetMode,
)

ROOT = Path(__file__).resolve().parents[1]
TS_FIXTURE = ROOT / "tests" / "fixtures" / "ts_project"
TS_QUALITY_FIXTURE = ROOT / "tests" / "fixtures" / "ts_quality"


def test_typescript_analyzer_builds_context_pack(built_ts_analyzer: None) -> None:
    diff = parse_unified_diff((TS_FIXTURE / "cart.diff").read_text(encoding="utf-8"), TargetMode.PATCH)
    diff = classify_diff(diff, ignore_patterns=[])

    result = run_typescript_analyzer(TS_FIXTURE, diff.files)

    assert result is not None
    assert result.files[0].path == "src/cart.ts"
    assert result.files[0].changed_symbols[0].name == "calculateTotal"
    assert result.files[0].changed_symbols[0].references
    assert "tests/cart.test.ts" in result.files[0].related_tests

    packs = build_context_packs([result], diff.files, ReviewConfig(), repo_root=TS_FIXTURE)

    assert len(packs) == 1
    assert packs[0].symbol is not None
    assert packs[0].symbol.name == "calculateTotal"
    assert packs[0].references
    assert any("Changed symbols:" in note and "calculateTotal" in note for note in packs[0].impact_notes)
    assert any("Reference impact:" in note and "call=" in note for note in packs[0].impact_notes)
    assert any("Related tests:" in note and "tests/cart.test.ts" in note for note in packs[0].impact_notes)
    assert len(packs[0].changed_snippets) == 1
    assert packs[0].changed_snippets[0].file == "src/cart.ts"
    assert "export function calculateTotal" in packs[0].changed_snippets[0].code
    assert packs[0].reference_snippets
    assert packs[0].related_test_snippets
    assert packs[0].stats.diff_lines == len(packs[0].diff_snippet)
    assert packs[0].stats.estimated_chars > 0
    assert packs[0].stats.policy_key


def test_python_analyzer_builds_context_pack(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "calculator.py").write_text(
        "from decimal import Decimal\n\n"
        "RATE: Decimal = Decimal('1.10')\n\n"
        "def helper(value: Decimal) -> Decimal:\n"
        "    return value * RATE\n\n"
        "def calculate_total(price: Decimal, quantity: int) -> Decimal:\n"
        "    subtotal = price * quantity\n"
        "    return helper(subtotal)\n",
        encoding="utf-8",
    )
    (tmp_path / "tests" / "test_calculator.py").write_text(
        "from decimal import Decimal\n"
        "from calculator import calculate_total\n\n"
        "def test_calculate_total() -> None:\n"
        "    assert calculate_total(Decimal('2'), 3)\n",
        encoding="utf-8",
    )
    diff = parse_unified_diff(
        """diff --git a/src/calculator.py b/src/calculator.py
--- a/src/calculator.py
+++ b/src/calculator.py
@@ -8,3 +8,3 @@
 def calculate_total(price: Decimal, quantity: int) -> Decimal:
-    subtotal = price
+    subtotal = price * quantity
     return helper(subtotal)
""",
        TargetMode.PATCH,
    )
    diff = classify_diff(diff, ignore_patterns=[])

    result = run_python_analyzer(tmp_path, diff.files)
    packs = build_context_packs([result], diff.files, ReviewConfig(), repo_root=tmp_path) if result else []

    assert len(packs) == 1
    assert packs[0].symbol is not None
    assert packs[0].symbol.name == "calculate_total"
    assert packs[0].imports == ["from decimal import Decimal"]
    assert {"RATE", "helper", "calculate_total"} <= set(packs[0].exports)
    assert packs[0].related_tests == ["tests/test_calculator.py"]
    assert any("Changed symbols:" in note and "calculate_total" in note for note in packs[0].impact_notes)
    assert any("Related tests:" in note and "tests/test_calculator.py" in note for note in packs[0].impact_notes)
    assert packs[0].changed_snippets[0].file == "src/calculator.py"
    assert "def calculate_total" in packs[0].changed_snippets[0].code
    assert packs[0].related_test_snippets[0].file == "tests/test_calculator.py"
    assert "calculate_total(Decimal('2'), 3)" in packs[0].related_test_snippets[0].code


def test_python_context_pack_includes_reference_and_callee_snippets(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "pricing.py").write_text(
        "def apply_discount(amount: int) -> int:\n"
        "    return amount\n\n"
        "def calculate_total(price: int, quantity: int) -> int:\n"
        "    subtotal = price * quantity\n"
        "    return apply_discount(subtotal)\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "checkout.py").write_text(
        "from pricing import calculate_total as total_for_cart\n\n"
        "def checkout(price: int, quantity: int) -> int:\n"
        "    return total_for_cart(price, quantity)\n",
        encoding="utf-8",
    )
    diff = parse_unified_diff(
        """diff --git a/src/pricing.py b/src/pricing.py
--- a/src/pricing.py
+++ b/src/pricing.py
@@ -5,2 +5,2 @@
-    subtotal = price
+    subtotal = price * quantity
     return apply_discount(subtotal)
""",
        TargetMode.PATCH,
    )
    diff = classify_diff(diff, ignore_patterns=[])

    result = run_python_analyzer(tmp_path, diff.files)
    packs = build_context_packs([result], diff.files, ReviewConfig(), repo_root=tmp_path) if result else []

    assert len(packs) == 1
    assert any(
        "Reference impact:" in note and "1 non-import usage references" in note for note in packs[0].impact_notes
    )
    assert any("Callee contracts:" in note and "1 called definitions" in note for note in packs[0].impact_notes)
    assert packs[0].reference_snippets[0].file == "src/checkout.py"
    assert "return total_for_cart(price, quantity)" in packs[0].reference_snippets[0].code
    assert packs[0].callee_snippets[0].file == "src/pricing.py"
    assert "def apply_discount(amount: int) -> int:" in packs[0].callee_snippets[0].code


def test_context_pack_includes_matching_custom_rule() -> None:
    diff = parse_unified_diff((TS_FIXTURE / "cart.diff").read_text(encoding="utf-8"), TargetMode.PATCH)
    diff = classify_diff(diff, ignore_patterns=[])
    result = run_typescript_analyzer(TS_FIXTURE, diff.files)
    config = ReviewConfig(
        rule_definitions=[
            ReviewRule(
                id="cart-total",
                title="Preserve cart totals",
                severity="high",
                paths=["src/cart.ts"],
                triggers={"symbols": ["calculateTotal"]},
                body="Cart total changes must preserve quantity multiplication.",
            )
        ]
    )

    packs = build_context_packs([result], diff.files, config, repo_root=TS_FIXTURE) if result else []

    assert len(packs) == 1
    assert packs[0].rule_matches[0].id == "cart-total"
    assert "[custom-rule:cart-total]" in packs[0].rules[0]
    assert "quantity multiplication" in packs[0].rules[0]


def test_context_pack_includes_matching_memory_card(built_ts_analyzer: None) -> None:
    diff = parse_unified_diff((TS_FIXTURE / "cart.diff").read_text(encoding="utf-8"), TargetMode.PATCH)
    diff = classify_diff(diff, ignore_patterns=[])
    result = run_typescript_analyzer(TS_FIXTURE, diff.files)
    config = ReviewConfig(
        memory_definitions=[
            MemoryCard(
                id="cart-total-memory",
                title="Preserve cart totals",
                kind="invariant",
                severity="high",
                paths=["src/cart.ts"],
                triggers={"symbols": ["calculateTotal"]},
                body="Cart total changes must preserve quantity multiplication.",
            )
        ]
    )

    packs = build_context_packs([result], diff.files, config, repo_root=TS_FIXTURE) if result else []

    assert len(packs) == 1
    assert packs[0].memory_matches[0].id == "cart-total-memory"
    assert "quantity multiplication" in packs[0].memory_matches[0].rendered
    assert packs[0].stats.memory_cards == 1
    assert packs[0].stats.memory_chars == packs[0].memory_matches[0].prompt_chars


def test_build_context_packs_adds_diff_fallback_for_deleted_file() -> None:
    diff_text = """diff --git a/src/permissions.ts b/src/permissions.ts
deleted file mode 100644
--- a/src/permissions.ts
+++ /dev/null
@@ -1,3 +0,0 @@
-export enum Permission {
-  ADMIN = 'ADMIN',
-}
"""
    diff = parse_unified_diff(diff_text, TargetMode.PATCH)
    diff = classify_diff(diff, ignore_patterns=[])

    packs = build_context_packs([], diff.files, ReviewConfig(), repo_root=TS_FIXTURE)

    assert len(packs) == 1
    assert packs[0].id == "src/permissions.ts#diff"
    assert packs[0].file == "src/permissions.ts"
    assert "-export enum Permission {" in packs[0].diff_snippet
    assert packs[0].changed_snippets == []
    assert any("file-level change" in note for note in packs[0].impact_notes)


def test_build_context_packs_adds_diff_fallback_for_config_change() -> None:
    diff_text = """diff --git a/package.json b/package.json
--- a/package.json
+++ b/package.json
@@ -1,3 +1,3 @@
 {
-  "name": "old"
+  "name": "new"
 }
"""
    diff = parse_unified_diff(diff_text, TargetMode.PATCH)
    diff = classify_diff(diff, ignore_patterns=[])

    packs = build_context_packs([], diff.files, ReviewConfig(), repo_root=TS_FIXTURE)

    assert len(packs) == 1
    assert packs[0].id == "package.json#diff"
    assert packs[0].risk_signals
    assert '+  "name": "new"' in packs[0].diff_snippet


def test_build_context_packs_preserves_fallback_reason_warning() -> None:
    diff_text = """diff --git a/src/permissions.ts b/src/permissions.ts
--- a/src/permissions.ts
+++ b/src/permissions.ts
@@ -1,3 +1,3 @@
 export function canAccess(role: string): boolean {
-  return role === 'admin';
+  return role === 'admin' || role === 'support';
 }
"""
    diff = parse_unified_diff(diff_text, TargetMode.PATCH)
    diff = classify_diff(diff, ignore_patterns=[])

    packs = build_context_packs(
        [],
        diff.files,
        ReviewConfig(),
        repo_root=TS_FIXTURE,
        fallback_reasons_by_path={
            "src/permissions.ts": "TypeScript analyzer shard failed; using diff-only fallback context."
        },
    )

    assert len(packs) == 1
    assert packs[0].warnings == ["TypeScript analyzer shard failed; using diff-only fallback context."]


def test_seeded_bug_fixture_targets_calculate_total(built_ts_analyzer: None) -> None:
    diff = parse_unified_diff((TS_FIXTURE / "cart_bug.diff").read_text(encoding="utf-8"), TargetMode.PATCH)
    diff = classify_diff(diff, ignore_patterns=[])

    result = run_typescript_analyzer(TS_FIXTURE, diff.files)
    packs = build_context_packs([result], diff.files, ReviewConfig(), repo_root=TS_FIXTURE) if result else []

    assert len(packs) == 1
    assert packs[0].symbol is not None
    assert packs[0].symbol.name == "calculateTotal"
    assert packs[0].related_tests == ["tests/cart.test.ts"]
    assert packs[0].related_test_snippets[0].file == "tests/cart.test.ts"
    assert "calculateTotal" in packs[0].related_test_snippets[0].code
    assert any(
        snippet.file == "src/checkout.ts" and "total: calculateTotal(items)" in snippet.code
        for snippet in packs[0].reference_snippets
    )
    assert all(snippet.file != "tests/cart.test.ts" for snippet in packs[0].reference_snippets)
    assert "-  return items.reduce((sum, item) => sum + item.price * item.quantity, 0);" in packs[0].diff_snippet
    assert "+  return items.reduce((sum, item) => sum + item.price, 0);" in packs[0].diff_snippet


def test_typescript_analyzer_collects_same_file_call_references(built_ts_analyzer: None) -> None:
    fixture = TS_QUALITY_FIXTURE / "tenant_cache_leak"
    repo = fixture / "repo"
    diff = parse_unified_diff((fixture / "change.diff").read_text(encoding="utf-8"), TargetMode.PATCH)
    diff = classify_diff(diff, ignore_patterns=[])

    result = run_typescript_analyzer(repo, diff.files)
    packs = build_context_packs([result], diff.files, ReviewConfig(), repo_root=repo) if result else []

    assert result is not None
    symbol = result.files[0].changed_symbols[0]
    assert symbol.name == "settingsCacheKey"
    assert any(
        reference.file == "src/settings.ts"
        and reference.kind == "call"
        and "const key = settingsCacheKey(tenantId, userId);" in reference.text
        for reference in symbol.references
    )
    assert len(packs) == 1
    assert any(
        snippet.file == "src/settings.ts" and "const key = settingsCacheKey(tenantId, userId);" in snippet.code
        for snippet in packs[0].reference_snippets
    )


def test_typescript_analyzer_collects_callee_contract_snippets(
    tmp_path: Path,
    built_ts_analyzer: None,
) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (tmp_path / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler","strict":true},'
        '"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (src / "signature.ts").write_text(
        "export function verifyWebhookSignature(rawBody: string, signature: string): boolean {\n"
        "  return rawBody.startsWith('{') && signature.length > 0;\n"
        "}\n",
        encoding="utf-8",
    )
    (src / "controller.ts").write_text(
        "import { verifyWebhookSignature } from './signature.js';\n"
        "export function handleWebhook(body: unknown, signature: string): boolean {\n"
        "  return verifyWebhookSignature(JSON.stringify(body), signature);\n"
        "}\n",
        encoding="utf-8",
    )
    diff = parse_unified_diff(
        """diff --git a/src/controller.ts b/src/controller.ts
index 1111111..2222222 100644
--- a/src/controller.ts
+++ b/src/controller.ts
@@ -1,4 +1,4 @@
 import { verifyWebhookSignature } from './signature.js';
-export function handleWebhook(rawBody: string, signature: string): boolean {
-  return verifyWebhookSignature(rawBody, signature);
+export function handleWebhook(body: unknown, signature: string): boolean {
+  return verifyWebhookSignature(JSON.stringify(body), signature);
 }
""",
        TargetMode.PATCH,
    )
    diff = classify_diff(diff, ignore_patterns=[])

    result = run_typescript_analyzer(tmp_path, diff.files)
    packs = build_context_packs([result], diff.files, ReviewConfig(), repo_root=tmp_path) if result else []

    assert result is not None
    symbol = result.files[0].changed_symbols[0]
    assert symbol.name == "handleWebhook"
    assert any(
        callee.file == "src/signature.ts" and callee.kind == "callee" and "verifyWebhookSignature" in callee.text
        for callee in symbol.callees
    )
    assert len(packs) == 1
    assert any(
        snippet.file == "src/signature.ts"
        and "verifyWebhookSignature(rawBody: string, signature: string)" in snippet.code
        for snippet in packs[0].callee_snippets
    )
    assert any("Callee contracts:" in note for note in packs[0].impact_notes)


def test_typescript_analyzer_collects_framework_metadata_for_method_body_change(
    tmp_path: Path,
    built_ts_analyzer: None,
) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (tmp_path / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler","strict":true,'
        '"experimentalDecorators":true},"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (src / "admin-webhook.controller.ts").write_text(
        "function Controller(_path: string): ClassDecorator {\n"
        "  return () => undefined;\n"
        "}\n"
        "function UseGuards(..._guards: unknown[]): ClassDecorator {\n"
        "  return () => undefined;\n"
        "}\n"
        "function Post(_path: string): MethodDecorator {\n"
        "  return () => undefined;\n"
        "}\n"
        "function RequirePermission(_permission: Permission): MethodDecorator {\n"
        "  return () => undefined;\n"
        "}\n"
        "class OperatorAuthGuard {}\n"
        "class PermissionGuard {}\n"
        "export enum Permission {\n"
        "  WEBHOOK_INBOX_VIEW = 'WEBHOOK_INBOX_VIEW',\n"
        "  WEBHOOK_RETRIGGER = 'WEBHOOK_RETRIGGER',\n"
        "}\n"
        "@Controller('admin/webhooks/inbox')\n"
        "@UseGuards(OperatorAuthGuard, PermissionGuard)\n"
        "export class AdminWebhookController {\n"
        "  @Post(':id/retrigger')\n"
        "  @RequirePermission(Permission.WEBHOOK_RETRIGGER)\n"
        "  retrigger(id: string, actorId: string): string {\n"
        "    return id;\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    diff = parse_unified_diff(
        """diff --git a/src/admin-webhook.controller.ts b/src/admin-webhook.controller.ts
index 1111111..2222222 100644
--- a/src/admin-webhook.controller.ts
+++ b/src/admin-webhook.controller.ts
@@ -22,6 +22,6 @@ export class AdminWebhookController {
   @Post(':id/retrigger')
   @RequirePermission(Permission.WEBHOOK_RETRIGGER)
   retrigger(id: string, actorId: string): string {
-    return `${actorId}:${id}`;
+    return id;
   }
 }
""",
        TargetMode.PATCH,
    )
    diff = classify_diff(diff, ignore_patterns=[])

    result = run_typescript_analyzer(tmp_path, diff.files)
    packs = build_context_packs([result], diff.files, ReviewConfig(), repo_root=tmp_path) if result else []

    assert result is not None
    symbols_by_name = {symbol.name: symbol for symbol in result.files[0].changed_symbols}
    symbol = symbols_by_name["retrigger"]
    metadata_texts = [reference.text for reference in symbol.metadata if reference.kind == "metadata"]
    assert "@Controller('admin/webhooks/inbox')" in metadata_texts
    assert "@UseGuards(OperatorAuthGuard, PermissionGuard)" in metadata_texts
    assert "@Post(':id/retrigger')" in metadata_texts
    assert "@RequirePermission(Permission.WEBHOOK_RETRIGGER)" in metadata_texts
    assert len(packs) == 1
    assert any("@Controller('admin/webhooks/inbox')" in snippet.code for snippet in packs[0].metadata_snippets)
    assert any(
        "@RequirePermission(Permission.WEBHOOK_RETRIGGER)" in snippet.code for snippet in packs[0].metadata_snippets
    )
    assert any("Framework metadata:" in note for note in packs[0].impact_notes)


def test_typescript_analyzer_collects_decorator_argument_contracts(
    tmp_path: Path,
    built_ts_analyzer: None,
) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (tmp_path / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler","strict":true,'
        '"experimentalDecorators":true},"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (src / "permissions.ts").write_text(
        "export enum Permission {\n"
        "  WEBHOOK_INBOX_VIEW = 'WEBHOOK_INBOX_VIEW',\n"
        "  WEBHOOK_RETRIGGER = 'WEBHOOK_RETRIGGER',\n"
        "}\n",
        encoding="utf-8",
    )
    (src / "guards.ts").write_text(
        "export class PermissionGuard {\n"
        "  /** Requires a Permission metadata value to be present on the handler. */\n"
        "  canActivate(): boolean {\n"
        "    return true;\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    (src / "admin-webhook.controller.ts").write_text(
        "import { Permission } from './permissions.js';\n"
        "import { PermissionGuard } from './guards.js';\n"
        "function Controller(_path: string): ClassDecorator {\n"
        "  return () => undefined;\n"
        "}\n"
        "function UseGuards(..._guards: unknown[]): ClassDecorator {\n"
        "  return () => undefined;\n"
        "}\n"
        "function Post(_path: string): MethodDecorator {\n"
        "  return () => undefined;\n"
        "}\n"
        "function RequirePermission(_permission: Permission): MethodDecorator {\n"
        "  return () => undefined;\n"
        "}\n"
        "@Controller('admin/webhooks/inbox')\n"
        "@UseGuards(PermissionGuard)\n"
        "export class AdminWebhookController {\n"
        "  @Post(':id/retrigger')\n"
        "  @RequirePermission(Permission.WEBHOOK_RETRIGGER)\n"
        "  retrigger(id: string, actorId: string): string {\n"
        "    return id;\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    diff = parse_unified_diff(
        """diff --git a/src/admin-webhook.controller.ts b/src/admin-webhook.controller.ts
index 1111111..2222222 100644
--- a/src/admin-webhook.controller.ts
+++ b/src/admin-webhook.controller.ts
@@ -18,6 +18,6 @@ export class AdminWebhookController {
   @Post(':id/retrigger')
   @RequirePermission(Permission.WEBHOOK_RETRIGGER)
   retrigger(id: string, actorId: string): string {
-    return `${actorId}:${id}`;
+    return id;
   }
 }
""",
        TargetMode.PATCH,
    )
    diff = classify_diff(diff, ignore_patterns=[])

    result = run_typescript_analyzer(tmp_path, diff.files)
    packs = build_context_packs([result], diff.files, ReviewConfig(), repo_root=tmp_path) if result else []

    assert result is not None
    symbol = {symbol.name: symbol for symbol in result.files[0].changed_symbols}["retrigger"]
    assert any(
        contract.file == "src/permissions.ts" and contract.kind == "contract" and "Permission" in contract.text
        for contract in symbol.contracts
    )
    assert any(
        contract.file == "src/guards.ts" and contract.kind == "contract" and "PermissionGuard" in contract.text
        for contract in symbol.contracts
    )
    assert len(packs) == 1
    assert any("WEBHOOK_RETRIGGER = 'WEBHOOK_RETRIGGER'" in snippet.code for snippet in packs[0].contract_snippets)
    assert any("Requires a Permission metadata value" in snippet.code for snippet in packs[0].contract_snippets)


def test_typescript_analyzer_collects_decorator_factory_contracts(
    tmp_path: Path,
    built_ts_analyzer: None,
) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (tmp_path / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler","strict":true,'
        '"experimentalDecorators":true},"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (src / "permissions.ts").write_text(
        "export enum Permission {\n  WEBHOOK_RETRIGGER = 'WEBHOOK_RETRIGGER',\n}\n",
        encoding="utf-8",
    )
    (src / "require-permission.decorator.ts").write_text(
        "import { Permission } from './permissions.js';\n"
        "\n"
        "export const REQUIRED_PERMISSION_KEY = 'required_permission';\n"
        "\n"
        "function SetMetadata(_key: string, _value: unknown): MethodDecorator {\n"
        "  return () => undefined;\n"
        "}\n"
        "\n"
        "export function RequirePermission(permission: Permission): MethodDecorator {\n"
        "  return SetMetadata(REQUIRED_PERMISSION_KEY, permission);\n"
        "}\n",
        encoding="utf-8",
    )
    (src / "admin-webhook.controller.ts").write_text(
        "import { Permission } from './permissions.js';\n"
        "import { RequirePermission } from './require-permission.decorator.js';\n"
        "function Controller(_path: string): ClassDecorator {\n"
        "  return () => undefined;\n"
        "}\n"
        "function Post(_path: string): MethodDecorator {\n"
        "  return () => undefined;\n"
        "}\n"
        "@Controller('admin/webhooks/inbox')\n"
        "export class AdminWebhookController {\n"
        "  @Post(':id/retrigger')\n"
        "  @RequirePermission(Permission.WEBHOOK_RETRIGGER)\n"
        "  retrigger(id: string, actorId: string): string {\n"
        "    return id;\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    diff = parse_unified_diff(
        """diff --git a/src/admin-webhook.controller.ts b/src/admin-webhook.controller.ts
index 1111111..2222222 100644
--- a/src/admin-webhook.controller.ts
+++ b/src/admin-webhook.controller.ts
@@ -12,6 +12,6 @@ export class AdminWebhookController {
   @Post(':id/retrigger')
   @RequirePermission(Permission.WEBHOOK_RETRIGGER)
   retrigger(id: string, actorId: string): string {
-    return `${actorId}:${id}`;
+    return id;
   }
 }
""",
        TargetMode.PATCH,
    )
    diff = classify_diff(diff, ignore_patterns=[])

    result = run_typescript_analyzer(tmp_path, diff.files)
    packs = build_context_packs([result], diff.files, ReviewConfig(), repo_root=tmp_path) if result else []

    assert result is not None
    symbol = {symbol.name: symbol for symbol in result.files[0].changed_symbols}["retrigger"]
    assert any(
        contract.file == "src/require-permission.decorator.ts"
        and contract.kind == "contract"
        and "RequirePermission" in contract.text
        for contract in symbol.contracts
    )
    assert len(packs) == 1
    assert any("REQUIRED_PERMISSION_KEY" in snippet.code for snippet in packs[0].contract_snippets)


def test_typescript_analyzer_collects_decorator_metadata_key_consumers(
    tmp_path: Path,
    built_ts_analyzer: None,
) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (tmp_path / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler","strict":true,'
        '"experimentalDecorators":true},"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (src / "permissions.ts").write_text(
        "export enum Permission {\n  WEBHOOK_RETRIGGER = 'WEBHOOK_RETRIGGER',\n}\n",
        encoding="utf-8",
    )
    (src / "require-permission.decorator.ts").write_text(
        "import { Permission } from './permissions.js';\n"
        "\n"
        "export const REQUIRED_PERMISSION_KEY = 'required_permission';\n"
        "\n"
        "function SetMetadata(_key: string, _value: unknown): MethodDecorator {\n"
        "  return () => undefined;\n"
        "}\n"
        "\n"
        "export function RequirePermission(permission: Permission): MethodDecorator {\n"
        "  return SetMetadata(REQUIRED_PERMISSION_KEY, permission);\n"
        "}\n",
        encoding="utf-8",
    )
    (src / "permission.guard.ts").write_text(
        "import { Permission } from './permissions.js';\n"
        "import { REQUIRED_PERMISSION_KEY } from './require-permission.decorator.js';\n"
        "\n"
        "interface ExecutionContext {\n"
        "  getHandler(): unknown;\n"
        "  getClass(): unknown;\n"
        "}\n"
        "\n"
        "class Reflector {\n"
        "  getAllAndOverride<T>(_key: string, _targets: unknown[]): T | undefined {\n"
        "    return undefined;\n"
        "  }\n"
        "}\n"
        "\n"
        "export class PermissionGuard {\n"
        "  private readonly reflector = new Reflector();\n"
        "\n"
        "  canActivate(context: ExecutionContext): boolean {\n"
        "    const requiredPermission = this.reflector.getAllAndOverride<Permission>(REQUIRED_PERMISSION_KEY, [\n"
        "      context.getHandler(),\n"
        "      context.getClass(),\n"
        "    ]);\n"
        "    return Boolean(requiredPermission);\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    (src / "admin-webhook.controller.ts").write_text(
        "import { Permission } from './permissions.js';\n"
        "import { RequirePermission } from './require-permission.decorator.js';\n"
        "function Controller(_path: string): ClassDecorator {\n"
        "  return () => undefined;\n"
        "}\n"
        "function Post(_path: string): MethodDecorator {\n"
        "  return () => undefined;\n"
        "}\n"
        "@Controller('admin/webhooks/inbox')\n"
        "export class AdminWebhookController {\n"
        "  @Post(':id/retrigger')\n"
        "  @RequirePermission(Permission.WEBHOOK_RETRIGGER)\n"
        "  retrigger(id: string, actorId: string): string {\n"
        "    return id;\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    diff = parse_unified_diff(
        """diff --git a/src/admin-webhook.controller.ts b/src/admin-webhook.controller.ts
index 1111111..2222222 100644
--- a/src/admin-webhook.controller.ts
+++ b/src/admin-webhook.controller.ts
@@ -12,6 +12,6 @@ export class AdminWebhookController {
   @Post(':id/retrigger')
   @RequirePermission(Permission.WEBHOOK_RETRIGGER)
   retrigger(id: string, actorId: string): string {
-    return `${actorId}:${id}`;
+    return id;
   }
 }
""",
        TargetMode.PATCH,
    )
    diff = classify_diff(diff, ignore_patterns=[])

    result = run_typescript_analyzer(tmp_path, diff.files)
    packs = build_context_packs([result], diff.files, ReviewConfig(), repo_root=tmp_path) if result else []

    assert result is not None
    symbol = {symbol.name: symbol for symbol in result.files[0].changed_symbols}["retrigger"]
    assert any(
        contract.file == "src/permission.guard.ts"
        and contract.kind == "contract"
        and "PermissionGuard" in contract.text
        for contract in symbol.contracts
    )
    assert len(packs) == 1
    assert any(
        "getAllAndOverride<Permission>(REQUIRED_PERMISSION_KEY" in snippet.code
        for snippet in packs[0].contract_snippets
    )


def test_typescript_analyzer_collects_schema_contract_snippets_for_parse_calls(
    tmp_path: Path,
    built_ts_analyzer: None,
) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (tmp_path / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler","strict":true},'
        '"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (src / "quote-schemas.ts").write_text(
        "const stringSchema = {\n"
        "  trim: () => stringSchema,\n"
        "  min: (_value: number) => stringSchema,\n"
        "  max: (_value: number) => stringSchema,\n"
        "};\n"
        "const arraySchema = {\n"
        "  min: (_value: number) => arraySchema,\n"
        "  max: (_value: number) => arraySchema,\n"
        "};\n"
        "const z = {\n"
        "  string: () => stringSchema,\n"
        "  array: (_schema: unknown) => arraySchema,\n"
        "  object: <T extends object>(_shape: T) => ({ parse: (value: unknown): T => value as T }),\n"
        "};\n"
        "export const AddQuoteSchema = z.object({\n"
        "  lpProvider: z.string().trim().min(1).max(100),\n"
        "  reason: z.string().trim().min(1).max(500),\n"
        "  fileIds: z.array(z.string()).min(1).max(10),\n"
        "});\n",
        encoding="utf-8",
    )
    (src / "quote-controller.ts").write_text(
        "import { AddQuoteSchema } from './quote-schemas.js';\n"
        "export function addQuote(body: unknown): { reason: unknown; fileCount: number } {\n"
        "  const parsed = AddQuoteSchema.parse(body);\n"
        "  return { reason: parsed.reason, fileCount: 0 };\n"
        "}\n",
        encoding="utf-8",
    )
    diff = parse_unified_diff(
        """diff --git a/src/quote-controller.ts b/src/quote-controller.ts
index 1111111..2222222 100644
--- a/src/quote-controller.ts
+++ b/src/quote-controller.ts
@@ -1,5 +1,5 @@
 import { AddQuoteSchema } from './quote-schemas.js';
 export function addQuote(body: unknown): { reason: unknown; fileCount: number } {
   const parsed = AddQuoteSchema.parse(body);
-  return { reason: parsed.reason, fileCount: parsed.fileIds.length };
+  return { reason: parsed.reason, fileCount: 0 };
 }
""",
        TargetMode.PATCH,
    )
    diff = classify_diff(diff, ignore_patterns=[])

    result = run_typescript_analyzer(tmp_path, diff.files)
    packs = build_context_packs([result], diff.files, ReviewConfig(), repo_root=tmp_path) if result else []

    assert result is not None
    symbol = result.files[0].changed_symbols[0]
    assert symbol.name == "addQuote"
    assert any(
        contract.file == "src/quote-schemas.ts"
        and contract.kind == "contract"
        and "AddQuoteSchema" in contract.text
        and contract.end_line is not None
        for contract in symbol.contracts
    )
    assert len(packs) == 1
    assert any(
        snippet.file == "src/quote-schemas.ts" and "fileIds: z.array(z.string()).min(1).max(10)" in snippet.code
        for snippet in packs[0].contract_snippets
    )
    assert any("Contract context:" in note for note in packs[0].impact_notes)


def test_typescript_analyzer_collects_schema_factory_contract_for_parse_calls(
    tmp_path: Path,
    built_ts_analyzer: None,
) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (tmp_path / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler","strict":true},'
        '"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (src / "audit-schema.ts").write_text(
        "const z = {\n"
        "  enum: <T extends readonly string[]>(_values: T) => ({ parse: (value: unknown): T[number] => value as T[number] }),\n"
        "  object: <T extends object>(_shape: T) => ({ parse: (value: unknown): T => value as T }),\n"
        "};\n"
        "\n"
        "export function typedAuditEntrySchema() {\n"
        "  return z.object({\n"
        "    actorType: z.enum(['SYSTEM', 'OPERATOR'] as const),\n"
        "    action: z.enum(['CREATE', 'DELETE'] as const),\n"
        "  });\n"
        "}\n",
        encoding="utf-8",
    )
    (src / "audit-event.ts").write_text(
        "import { typedAuditEntrySchema } from './audit-schema.js';\n"
        "\n"
        "export function buildAuditEvent(entry: unknown): { actorType: string; action: string } {\n"
        "  const parsed = typedAuditEntrySchema().parse(entry);\n"
        "  return { actorType: 'OPERATOR', action: parsed.action as string };\n"
        "}\n",
        encoding="utf-8",
    )
    diff = parse_unified_diff(
        """diff --git a/src/audit-event.ts b/src/audit-event.ts
index 1111111..2222222 100644
--- a/src/audit-event.ts
+++ b/src/audit-event.ts
@@ -2,6 +2,6 @@ import { typedAuditEntrySchema } from './audit-schema.js';

 export function buildAuditEvent(entry: unknown): { actorType: string; action: string } {
   const parsed = typedAuditEntrySchema().parse(entry);
-  return { actorType: parsed.actorType as string, action: parsed.action as string };
+  return { actorType: 'OPERATOR', action: parsed.action as string };
 }
""",
        TargetMode.PATCH,
    )
    diff = classify_diff(diff, ignore_patterns=[])

    result = run_typescript_analyzer(tmp_path, diff.files)
    packs = build_context_packs([result], diff.files, ReviewConfig(), repo_root=tmp_path) if result else []

    assert result is not None
    symbol = result.files[0].changed_symbols[0]
    assert symbol.name == "buildAuditEvent"
    assert any(
        contract.file == "src/audit-schema.ts"
        and contract.kind == "contract"
        and "typedAuditEntrySchema" in contract.text
        for contract in symbol.contracts
    )
    assert len(packs) == 1
    assert any(
        snippet.file == "src/audit-schema.ts" and "actorType: z.enum(['SYSTEM', 'OPERATOR'] as const)" in snippet.code
        for snippet in packs[0].contract_snippets
    )


def test_typescript_analyzer_collects_composed_schema_contracts(
    tmp_path: Path,
    built_ts_analyzer: None,
) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (tmp_path / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler","strict":true},'
        '"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (src / "step-payloads.ts").write_text(
        "export const STEP_TYPES = ['QUOTE_REQUESTED', 'QUOTE_PRICED'] as const;\n"
        "\n"
        "export const MoneySchema = {\n"
        "  parse: (value: unknown): { amount: string; currency: 'USD' | 'EUR' } =>\n"
        "    value as { amount: string; currency: 'USD' | 'EUR' },\n"
        "};\n"
        "\n"
        "export const BaseStepPayloadSchema = {\n"
        "  parse: (value: unknown): { stepType: (typeof STEP_TYPES)[number]; settlementId: string } =>\n"
        "    value as { stepType: (typeof STEP_TYPES)[number]; settlementId: string },\n"
        "  stepTypes: STEP_TYPES,\n"
        "};\n"
        "\n"
        "export const QuoteStepPayloadSchema = {\n"
        "  parse: (value: unknown): { stepType: (typeof STEP_TYPES)[number]; settlementId: string; money: ReturnType<typeof MoneySchema.parse> } =>\n"
        "    value as { stepType: (typeof STEP_TYPES)[number]; settlementId: string; money: ReturnType<typeof MoneySchema.parse> },\n"
        "  base: BaseStepPayloadSchema,\n"
        "  money: MoneySchema,\n"
        "};\n",
        encoding="utf-8",
    )
    (src / "mapper.ts").write_text(
        "import { QuoteStepPayloadSchema } from './step-payloads.js';\n"
        "export function mapQuotePayload(raw: unknown): string {\n"
        "  const parsed = QuoteStepPayloadSchema.parse(raw);\n"
        "  return parsed.settlementId;\n"
        "}\n",
        encoding="utf-8",
    )
    diff = parse_unified_diff(
        """diff --git a/src/mapper.ts b/src/mapper.ts
index 1111111..2222222 100644
--- a/src/mapper.ts
+++ b/src/mapper.ts
@@ -1,5 +1,5 @@
 import { QuoteStepPayloadSchema } from './step-payloads.js';
 export function mapQuotePayload(raw: unknown): string {
   const parsed = QuoteStepPayloadSchema.parse(raw);
-  return parsed.stepType;
+  return parsed.settlementId;
 }
""",
        TargetMode.PATCH,
    )
    diff = classify_diff(diff, ignore_patterns=[])

    result = run_typescript_analyzer(tmp_path, diff.files)
    packs = build_context_packs([result], diff.files, ReviewConfig(), repo_root=tmp_path) if result else []

    assert result is not None
    symbol = result.files[0].changed_symbols[0]
    assert symbol.name == "mapQuotePayload"
    assert any(
        contract.file == "src/step-payloads.ts"
        and contract.kind == "contract"
        and "BaseStepPayloadSchema" in contract.text
        for contract in symbol.contracts
    )
    assert any(
        contract.file == "src/step-payloads.ts" and contract.kind == "contract" and "STEP_TYPES" in contract.text
        for contract in symbol.contracts
    )
    assert any(
        contract.file == "src/step-payloads.ts" and contract.kind == "contract" and "MoneySchema" in contract.text
        for contract in symbol.contracts
    )
    assert len(packs) == 1
    assert any("'QUOTE_PRICED'" in snippet.code for snippet in packs[0].contract_snippets)
    assert any("settlementId: string" in snippet.code for snippet in packs[0].contract_snippets)
    assert any("currency: 'USD' | 'EUR'" in snippet.code for snippet in packs[0].contract_snippets)


def test_typescript_analyzer_collects_schema_argument_contract_for_client_calls(
    tmp_path: Path,
    built_ts_analyzer: None,
) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (tmp_path / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler","strict":true},'
        '"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (src / "schemas.ts").write_text(
        "export const CoreBankPersonSchema = {\n"
        "  parse: (value: unknown): { id: string; verificationStatus: number } =>\n"
        "    value as { id: string; verificationStatus: number },\n"
        "  shape: {\n"
        "    id: 'string',\n"
        "    verificationStatus: 'number',\n"
        "  },\n"
        "};\n",
        encoding="utf-8",
    )
    (src / "client.ts").write_text(
        "import { CoreBankPersonSchema } from './schemas.js';\n"
        "\n"
        "interface HttpClient {\n"
        "  get<T>(path: string, schema: { parse(value: unknown): T }): Promise<T>;\n"
        "}\n"
        "\n"
        "export async function loadPersonStatus(client: HttpClient, id: string): Promise<number> {\n"
        "  const person = await client.get(`/clients/${id}`, CoreBankPersonSchema);\n"
        "  return 0;\n"
        "}\n",
        encoding="utf-8",
    )
    diff = parse_unified_diff(
        """diff --git a/src/client.ts b/src/client.ts
index 1111111..2222222 100644
--- a/src/client.ts
+++ b/src/client.ts
@@ -6,6 +6,6 @@ interface HttpClient {

 export async function loadPersonStatus(client: HttpClient, id: string): Promise<number> {
   const person = await client.get(`/clients/${id}`, CoreBankPersonSchema);
-  return person.verificationStatus;
+  return 0;
 }
""",
        TargetMode.PATCH,
    )
    diff = classify_diff(diff, ignore_patterns=[])

    result = run_typescript_analyzer(tmp_path, diff.files)
    packs = build_context_packs([result], diff.files, ReviewConfig(), repo_root=tmp_path) if result else []

    assert result is not None
    symbol = result.files[0].changed_symbols[0]
    assert symbol.name == "loadPersonStatus"
    assert any(
        contract.file == "src/schemas.ts" and contract.kind == "contract" and "CoreBankPersonSchema" in contract.text
        for contract in symbol.contracts
    )
    assert len(packs) == 1
    assert any(
        snippet.file == "src/schemas.ts" and "verificationStatus: 'number'" in snippet.code
        for snippet in packs[0].contract_snippets
    )


def test_typescript_analyzer_collects_return_type_contract_snippets(
    tmp_path: Path,
    built_ts_analyzer: None,
) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (tmp_path / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler","strict":true},'
        '"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (src / "response.ts").write_text(
        "export interface WebhookInboxDetail {\n"
        "  id: string;\n"
        "  providerHeaders: Record<string, string> | null;\n"
        "}\n"
        "\n"
        "export interface ApiResponse<T> {\n"
        "  data: T;\n"
        "}\n",
        encoding="utf-8",
    )
    (src / "controller.ts").write_text(
        "import type { ApiResponse, WebhookInboxDetail } from './response.js';\n"
        "\n"
        "export function detail(id: string): ApiResponse<WebhookInboxDetail> {\n"
        "  return { data: { id, providerHeaders: null } };\n"
        "}\n",
        encoding="utf-8",
    )
    diff = parse_unified_diff(
        """diff --git a/src/controller.ts b/src/controller.ts
index 1111111..2222222 100644
--- a/src/controller.ts
+++ b/src/controller.ts
@@ -1,5 +1,5 @@
 import type { ApiResponse, WebhookInboxDetail } from './response.js';

 export function detail(id: string): ApiResponse<WebhookInboxDetail> {
-  return { data: { id, providerHeaders: {} } };
+  return { data: { id, providerHeaders: null } };
 }
""",
        TargetMode.PATCH,
    )
    diff = classify_diff(diff, ignore_patterns=[])

    result = run_typescript_analyzer(tmp_path, diff.files)
    packs = build_context_packs([result], diff.files, ReviewConfig(), repo_root=tmp_path) if result else []

    assert result is not None
    symbol = result.files[0].changed_symbols[0]
    assert symbol.name == "detail"
    assert any(
        contract.file == "src/response.ts" and contract.kind == "contract" and "WebhookInboxDetail" in contract.text
        for contract in symbol.contracts
    )
    assert len(packs) == 1
    assert any(
        snippet.file == "src/response.ts" and "providerHeaders: Record<string, string> | null" in snippet.code
        for snippet in packs[0].contract_snippets
    )


def test_typescript_analyzer_collects_declared_type_contract_dependencies(
    tmp_path: Path,
    built_ts_analyzer: None,
) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (tmp_path / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler","strict":true},'
        '"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (src / "public-quote.ts").write_text(
        "export interface PublicTransferPayloadBase {\n"
        "  targetsClientAccount: boolean;\n"
        "  providerAccountOwnerRef?: string;\n"
        "}\n",
        encoding="utf-8",
    )
    (src / "step-payloads.ts").write_text(
        "import type { PublicTransferPayloadBase } from './public-quote.js';\n"
        "\n"
        "export interface InternalTransferPayload extends PublicTransferPayloadBase {\n"
        "  providerAccountOwnerRef: string;\n"
        "  clientOwnerRef: string;\n"
        "}\n",
        encoding="utf-8",
    )
    (src / "mapper.ts").write_text(
        "import type { InternalTransferPayload } from './step-payloads.js';\n"
        "\n"
        "export function senderOwnerRef(payload: InternalTransferPayload): string {\n"
        "  return payload.clientOwnerRef;\n"
        "}\n",
        encoding="utf-8",
    )
    diff = parse_unified_diff(
        """diff --git a/src/mapper.ts b/src/mapper.ts
index 1111111..2222222 100644
--- a/src/mapper.ts
+++ b/src/mapper.ts
@@ -1,5 +1,5 @@
 import type { InternalTransferPayload } from './step-payloads.js';

 export function senderOwnerRef(payload: InternalTransferPayload): string {
-  return payload.providerAccountOwnerRef;
+  return payload.clientOwnerRef;
 }
""",
        TargetMode.PATCH,
    )
    diff = classify_diff(diff, ignore_patterns=[])

    result = run_typescript_analyzer(tmp_path, diff.files)
    packs = build_context_packs([result], diff.files, ReviewConfig(), repo_root=tmp_path) if result else []

    assert result is not None
    symbol = result.files[0].changed_symbols[0]
    assert symbol.name == "senderOwnerRef"
    assert any(
        contract.file == "src/step-payloads.ts"
        and contract.kind == "contract"
        and "InternalTransferPayload" in contract.text
        for contract in symbol.contracts
    )
    assert any(
        contract.file == "src/public-quote.ts"
        and contract.kind == "contract"
        and "PublicTransferPayloadBase" in contract.text
        for contract in symbol.contracts
    )
    assert len(packs) == 1
    assert any(
        snippet.file == "src/public-quote.ts" and "providerAccountOwnerRef?: string" in snippet.code
        for snippet in packs[0].contract_snippets
    )


def test_typescript_analyzer_collects_pick_property_contract_dependencies(
    tmp_path: Path,
    built_ts_analyzer: None,
) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (tmp_path / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler","strict":true},'
        '"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (src / "env.ts").write_text(
        "type Infer<T> = T extends { output: infer Output } ? Output : never;\n"
        "\n"
        "export const envSchema = {\n"
        "  output: {} as {\n"
        "    COREBANK_API_URL?: string;\n"
        "    COREBANK_TIMEOUT_MS: number;\n"
        "    COREBANK_RESPONSE_BODY_LOG_CHARS: number;\n"
        "  },\n"
        "  shape: {\n"
        "    STORAGE_BUCKET: 'string',\n"
        "    COREBANK_API_URL: 'url?',\n"
        "    COREBANK_TIMEOUT_MS: 'number?',\n"
        "    COREBANK_RESPONSE_BODY_LOG_CHARS: 'number',\n"
        "  },\n"
        "};\n"
        "export const validatedEnvSchema = envSchema;\n"
        "export type Env = Infer<typeof validatedEnvSchema>;\n",
        encoding="utf-8",
    )
    (src / "client-options.ts").write_text(
        "import type { Env } from './env.js';\n"
        "\n"
        "type CoreBankHttpClientEnv = Pick<\n"
        "  Env,\n"
        "  | 'COREBANK_API_URL'\n"
        "  | 'COREBANK_TIMEOUT_MS'\n"
        "  | 'COREBANK_RESPONSE_BODY_LOG_CHARS'\n"
        ">;\n"
        "\n"
        "export function buildCoreBankHttpClientOptions(env: CoreBankHttpClientEnv): number | undefined {\n"
        "  if ('COREBANK_API_URL' in env) return env.COREBANK_TIMEOUT_MS;\n"
        "  return undefined;\n"
        "}\n",
        encoding="utf-8",
    )
    diff = parse_unified_diff(
        """diff --git a/src/client-options.ts b/src/client-options.ts
index 1111111..2222222 100644
--- a/src/client-options.ts
+++ b/src/client-options.ts
@@ -8,6 +8,6 @@ type CoreBankHttpClientEnv = Pick<
 >;

 export function buildCoreBankHttpClientOptions(env: CoreBankHttpClientEnv): number | undefined {
-  return env.COREBANK_TIMEOUT_MS;
+  if ('COREBANK_API_URL' in env) return env.COREBANK_TIMEOUT_MS;
   return undefined;
 }
""",
        TargetMode.PATCH,
    )
    diff = classify_diff(diff, ignore_patterns=[])

    result = run_typescript_analyzer(tmp_path, diff.files)
    packs = build_context_packs([result], diff.files, ReviewConfig(), repo_root=tmp_path) if result else []

    assert result is not None
    symbol = result.files[0].changed_symbols[0]
    assert symbol.name == "buildCoreBankHttpClientOptions"
    assert any(
        contract.file == "src/env.ts" and contract.kind == "contract" and "COREBANK_TIMEOUT_MS:" in contract.text
        for contract in symbol.contracts
    )
    assert len(packs) == 1
    assert any(
        snippet.file == "src/env.ts" and "COREBANK_TIMEOUT_MS: 'number?'" in snippet.code
        for snippet in packs[0].contract_snippets
    )


def test_typescript_analyzer_collects_type_query_schema_contract_snippets(
    tmp_path: Path,
    built_ts_analyzer: None,
) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (tmp_path / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler","strict":true},'
        '"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (src / "schemas.ts").write_text(
        "export type Infer<T> = T extends { parse(value: unknown): infer Output } ? Output : never;\n"
        "\n"
        "export const CoreBankEmailSchema = {\n"
        "  parse: (value: unknown): { email: string; primary?: boolean } => value as { email: string; primary?: boolean },\n"
        "  shape: {\n"
        "    email: 'string',\n"
        "    primary: 'boolean?',\n"
        "  },\n"
        "};\n",
        encoding="utf-8",
    )
    (src / "mapper.ts").write_text(
        "import type { Infer } from './schemas.js';\n"
        "import { CoreBankEmailSchema } from './schemas.js';\n"
        "\n"
        "export function extractPrimaryEmail(emails: Infer<typeof CoreBankEmailSchema>[] | undefined): string {\n"
        "  const first = emails?.[0];\n"
        "  return first?.email ?? '';\n"
        "}\n",
        encoding="utf-8",
    )
    diff = parse_unified_diff(
        """diff --git a/src/mapper.ts b/src/mapper.ts
index 1111111..2222222 100644
--- a/src/mapper.ts
+++ b/src/mapper.ts
@@ -2,7 +2,7 @@ import type { Infer } from './schemas.js';
 import { CoreBankEmailSchema } from './schemas.js';

 export function extractPrimaryEmail(emails: Infer<typeof CoreBankEmailSchema>[] | undefined): string {
-  const primary = emails?.find((email) => email.primary);
-  return primary?.email ?? emails?.[0]?.email ?? '';
+  const first = emails?.[0];
+  return first?.email ?? '';
 }
""",
        TargetMode.PATCH,
    )
    diff = classify_diff(diff, ignore_patterns=[])

    result = run_typescript_analyzer(tmp_path, diff.files)
    packs = build_context_packs([result], diff.files, ReviewConfig(), repo_root=tmp_path) if result else []

    assert result is not None
    symbol = result.files[0].changed_symbols[0]
    assert symbol.name == "extractPrimaryEmail"
    assert any(
        contract.file == "src/schemas.ts" and contract.kind == "contract" and "CoreBankEmailSchema" in contract.text
        for contract in symbol.contracts
    )
    assert len(packs) == 1
    assert any(
        snippet.file == "src/schemas.ts" and "primary: 'boolean?'" in snippet.code
        for snippet in packs[0].contract_snippets
    )


def test_typescript_analyzer_collects_nest_request_surface_metadata(
    tmp_path: Path,
    built_ts_analyzer: None,
) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (tmp_path / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler","strict":true,'
        '"experimentalDecorators":true},"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (src / "dto.ts").write_text(
        "export class RetriggerBodyDto {\n  actorId!: string;\n  reason!: string;\n}\n",
        encoding="utf-8",
    )
    (src / "admin-webhook.controller.ts").write_text(
        "import { RetriggerBodyDto } from './dto.js';\n"
        "function Controller(_path: string): ClassDecorator {\n"
        "  return () => undefined;\n"
        "}\n"
        "function Post(_path: string): MethodDecorator {\n"
        "  return () => undefined;\n"
        "}\n"
        "function Param(_name: string): ParameterDecorator {\n"
        "  return () => undefined;\n"
        "}\n"
        "function Body(_pipe?: unknown): ParameterDecorator {\n"
        "  return () => undefined;\n"
        "}\n"
        "class ValidationPipe {}\n"
        "@Controller('admin/webhooks/inbox')\n"
        "export class AdminWebhookController {\n"
        "  @Post(':id/retrigger')\n"
        "  retrigger(\n"
        "    @Param('id') id: string,\n"
        "    @Body(new ValidationPipe()) body: RetriggerBodyDto,\n"
        "  ): string {\n"
        "    return id;\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    diff = parse_unified_diff(
        """diff --git a/src/admin-webhook.controller.ts b/src/admin-webhook.controller.ts
index 1111111..2222222 100644
--- a/src/admin-webhook.controller.ts
+++ b/src/admin-webhook.controller.ts
@@ -19,6 +19,6 @@ export class AdminWebhookController {
     @Body(new ValidationPipe()) body: RetriggerBodyDto,
   ): string {
-    return `${body.actorId}:${id}:${body.reason}`;
+    return id;
   }
 }
""",
        TargetMode.PATCH,
    )
    diff = classify_diff(diff, ignore_patterns=[])

    result = run_typescript_analyzer(tmp_path, diff.files)
    packs = build_context_packs([result], diff.files, ReviewConfig(), repo_root=tmp_path) if result else []

    assert result is not None
    symbols_by_name = {symbol.name: symbol for symbol in result.files[0].changed_symbols}
    symbol = symbols_by_name["retrigger"]
    metadata_texts = [reference.text for reference in symbol.metadata if reference.kind == "metadata"]
    assert "@Param('id') id: string," in metadata_texts
    assert "@Body(new ValidationPipe()) body: RetriggerBodyDto," in metadata_texts
    assert any(
        contract.file == "src/dto.ts" and contract.kind == "contract" and "RetriggerBodyDto" in contract.text
        for contract in symbol.contracts
    )
    assert len(packs) == 1
    assert any("@Param('id') id: string," in snippet.code for snippet in packs[0].metadata_snippets)
    assert any(
        "@Body(new ValidationPipe()) body: RetriggerBodyDto," in snippet.code for snippet in packs[0].metadata_snippets
    )
    assert any("actorId!: string" in snippet.code for snippet in packs[0].contract_snippets)


def test_typescript_analyzer_collects_dto_property_decorator_metadata(
    tmp_path: Path,
    built_ts_analyzer: None,
) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (tmp_path / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler","strict":true,'
        '"experimentalDecorators":true},"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (src / "dto.ts").write_text(
        "function IsString(): PropertyDecorator { return () => undefined; }\n"
        "function IsOptional(): PropertyDecorator { return () => undefined; }\n"
        "function MaxLength(_value: number): PropertyDecorator { return () => undefined; }\n"
        "export class RetriggerBodyDto {\n"
        "  @IsString()\n"
        "  actorId!: string;\n"
        "  @MaxLength(500)\n"
        "  reason?: string;\n"
        "}\n",
        encoding="utf-8",
    )
    diff = parse_unified_diff(
        """diff --git a/src/dto.ts b/src/dto.ts
index 1111111..2222222 100644
--- a/src/dto.ts
+++ b/src/dto.ts
@@ -5,5 +5,5 @@ export class RetriggerBodyDto {
   @IsString()
   actorId!: string;
-  @IsOptional()
+  @MaxLength(500)
   reason?: string;
 }
""",
        TargetMode.PATCH,
    )
    diff = classify_diff(diff, ignore_patterns=[])

    result = run_typescript_analyzer(tmp_path, diff.files)
    packs = build_context_packs([result], diff.files, ReviewConfig(), repo_root=tmp_path) if result else []

    assert result is not None
    symbol = {symbol.name: symbol for symbol in result.files[0].changed_symbols}["RetriggerBodyDto"]
    assert any(
        reference.kind == "metadata" and reference.file == "src/dto.ts" and "@MaxLength(500)" in reference.text
        for reference in symbol.metadata
    )
    assert len(packs) == 1
    assert any("@MaxLength(500)" in snippet.code for snippet in packs[0].metadata_snippets)


def test_typescript_analyzer_collects_constructor_injection_metadata_for_class_change(
    tmp_path: Path,
    built_ts_analyzer: None,
) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (tmp_path / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler","strict":true,'
        '"experimentalDecorators":true},"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (src / "operator-management.service.ts").write_text(
        "function Injectable(): ClassDecorator {\n"
        "  return () => undefined;\n"
        "}\n"
        "function Inject(_token: unknown): ParameterDecorator {\n"
        "  return () => undefined;\n"
        "}\n"
        "export const OPERATOR_REPOSITORY_PORT = Symbol('OPERATOR_REPOSITORY_PORT');\n"
        "export const AUDIT_REPOSITORY_PORT = Symbol('AUDIT_REPOSITORY_PORT');\n"
        "export interface OperatorRepositoryPort {\n"
        "  findById(id: string): Promise<string | null>;\n"
        "}\n"
        "\n"
        "@Injectable()\n"
        "export class OperatorManagementService {\n"
        "  constructor(@Inject(AUDIT_REPOSITORY_PORT) private readonly operators: OperatorRepositoryPort) {}\n"
        "\n"
        "  async findOperator(id: string): Promise<string | null> {\n"
        "    return this.operators.findById(id);\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    diff = parse_unified_diff(
        """diff --git a/src/operator-management.service.ts b/src/operator-management.service.ts
index 1111111..2222222 100644
--- a/src/operator-management.service.ts
+++ b/src/operator-management.service.ts
@@ -12,7 +12,7 @@ export interface OperatorRepositoryPort {

 @Injectable()
 export class OperatorManagementService {
-  constructor(@Inject(OPERATOR_REPOSITORY_PORT) private readonly operators: OperatorRepositoryPort) {}
+  constructor(@Inject(AUDIT_REPOSITORY_PORT) private readonly operators: OperatorRepositoryPort) {}

   async findOperator(id: string): Promise<string | null> {
     return this.operators.findById(id);
""",
        TargetMode.PATCH,
    )
    diff = classify_diff(diff, ignore_patterns=[])

    result = run_typescript_analyzer(tmp_path, diff.files)
    packs = build_context_packs([result], diff.files, ReviewConfig(), repo_root=tmp_path) if result else []

    assert result is not None
    symbol = {symbol.name: symbol for symbol in result.files[0].changed_symbols}["OperatorManagementService"]
    assert any(
        reference.kind == "metadata" and "@Inject(AUDIT_REPOSITORY_PORT)" in reference.text
        for reference in symbol.metadata
    )
    assert any(
        contract.kind == "contract"
        and contract.file == "src/operator-management.service.ts"
        and "OperatorRepositoryPort" in contract.text
        for contract in symbol.contracts
    )
    assert len(packs) == 1
    assert any("@Inject(AUDIT_REPOSITORY_PORT)" in snippet.code for snippet in packs[0].metadata_snippets)
    assert any("findById(id: string)" in snippet.code for snippet in packs[0].contract_snippets)


def test_typescript_analyzer_collects_class_heritage_contracts_for_class_change(
    tmp_path: Path,
    built_ts_analyzer: None,
) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (tmp_path / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler","strict":true},'
        '"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (src / "errors.ts").write_text(
        "export abstract class DomainError extends Error {\n"
        "  /** Keys copied to the public wire error detail payload. */\n"
        "  static readonly wireDetailKeys?: readonly string[];\n"
        "  detail?: Record<string, unknown>;\n"
        "}\n"
        "\n"
        "export abstract class BusinessRuleError extends DomainError {}\n",
        encoding="utf-8",
    )
    (src / "admin-webhook-ops.errors.ts").write_text(
        "import { BusinessRuleError } from './errors.js';\n"
        "export class WebhookRetriggerAlreadyInProgressError extends BusinessRuleError {\n"
        "  static readonly wireDetailKeys = [] as const;\n"
        "  constructor(public readonly retryAfterMs: number) {\n"
        "    super('Webhook retrigger is already in progress');\n"
        "    this.detail = { retryAfterMs };\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    diff = parse_unified_diff(
        "diff --git a/src/admin-webhook-ops.errors.ts b/src/admin-webhook-ops.errors.ts\n"
        "index 1111111..2222222 100644\n"
        "--- a/src/admin-webhook-ops.errors.ts\n"
        "+++ b/src/admin-webhook-ops.errors.ts\n"
        "@@ -1,7 +1,7 @@\n"
        " import { BusinessRuleError } from './errors.js';\n"
        " export class WebhookRetriggerAlreadyInProgressError extends BusinessRuleError {\n"
        "-  static readonly wireDetailKeys = ['retryAfterMs'] as const;\n"
        "+  static readonly wireDetailKeys = [] as const;\n"
        "   constructor(public readonly retryAfterMs: number) {\n"
        "     super('Webhook retrigger is already in progress');\n",
        TargetMode.PATCH,
    )
    diff = classify_diff(diff, ignore_patterns=[])

    result = run_typescript_analyzer(tmp_path, diff.files)
    packs = build_context_packs([result], diff.files, ReviewConfig(), repo_root=tmp_path) if result else []

    assert result is not None
    symbol = {symbol.name: symbol for symbol in result.files[0].changed_symbols}[
        "WebhookRetriggerAlreadyInProgressError"
    ]
    assert any(
        contract.file == "src/errors.ts" and contract.kind == "contract" and "BusinessRuleError" in contract.text
        for contract in symbol.contracts
    )
    assert any(
        contract.file == "src/errors.ts" and contract.kind == "contract" and "DomainError" in contract.text
        for contract in symbol.contracts
    )
    assert len(packs) == 1
    assert any(
        snippet.file == "src/errors.ts" and "Keys copied to the public wire error detail payload" in snippet.code
        for snippet in packs[0].contract_snippets
    )


def test_typescript_analyzer_collects_nest_module_provider_context(
    tmp_path: Path,
    built_ts_analyzer: None,
) -> None:
    src = tmp_path / "src"
    handlers = src / "handlers"
    handlers.mkdir(parents=True)
    (tmp_path / "package.json").write_text('{"name":"@acme/app","types":"./src/quote.module.ts"}', encoding="utf-8")
    (tmp_path / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler","strict":true,'
        '"experimentalDecorators":true},"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (src / "tokens.ts").write_text(
        "export const FLOW_HANDLER_PORT = Symbol('FLOW_HANDLER_PORT');\n",
        encoding="utf-8",
    )
    (src / "flow-handler.port.ts").write_text(
        "export interface FlowHandler {\n  supports(route: string): boolean;\n}\n",
        encoding="utf-8",
    )
    (handlers / "va-to-va-agent.handler.ts").write_text(
        "import type { FlowHandler } from '../flow-handler.port.js';\n"
        "export class VaToVaAgentHandler implements FlowHandler {\n"
        "  supports(route: string): boolean {\n"
        "    return true;\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    (src / "flow-handler.registry.ts").write_text(
        "import { FLOW_HANDLER_PORT } from './tokens.js';\n"
        "import type { FlowHandler } from './flow-handler.port.js';\n"
        "\n"
        "function Inject(_token: unknown): ParameterDecorator {\n"
        "  return () => undefined;\n"
        "}\n"
        "\n"
        "export class FlowHandlerRegistry {\n"
        "  constructor(@Inject(FLOW_HANDLER_PORT) private readonly handlers: ReadonlyArray<FlowHandler>) {}\n"
        "\n"
        "  all(): ReadonlyArray<FlowHandler> {\n"
        "    return this.handlers;\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    (src / "quote.module.ts").write_text(
        "import { VaToVaAgentHandler } from './handlers/va-to-va-agent.handler.js';\n"
        "import { FlowHandlerRegistry } from './flow-handler.registry.js';\n"
        "import { FLOW_HANDLER_PORT } from './tokens.js';\n"
        "\n"
        "function Module(_metadata: unknown): ClassDecorator {\n"
        "  return () => undefined;\n"
        "}\n"
        "\n"
        "@Module({\n"
        "  providers: [\n"
        "    VaToVaAgentHandler,\n"
        "    {\n"
        "      provide: FLOW_HANDLER_PORT,\n"
        "      useFactory: (handler: VaToVaAgentHandler) => [handler],\n"
        "      inject: [VaToVaAgentHandler],\n"
        "    },\n"
        "    FlowHandlerRegistry,\n"
        "  ],\n"
        "  exports: [FlowHandlerRegistry],\n"
        "})\n"
        "export class QuoteModule {}\n",
        encoding="utf-8",
    )
    diff = parse_unified_diff(
        """diff --git a/src/handlers/va-to-va-agent.handler.ts b/src/handlers/va-to-va-agent.handler.ts
index 1111111..2222222 100644
--- a/src/handlers/va-to-va-agent.handler.ts
+++ b/src/handlers/va-to-va-agent.handler.ts
@@ -1,6 +1,6 @@
 import type { FlowHandler } from '../flow-handler.port.js';
 export class VaToVaAgentHandler implements FlowHandler {
   supports(route: string): boolean {
-    return route === 'VA_TO_VA';
+    return true;
   }
 }
""",
        TargetMode.PATCH,
    )
    diff = classify_diff(diff, ignore_patterns=[])

    result = run_typescript_analyzer(tmp_path, diff.files)
    packs = build_context_packs([result], diff.files, ReviewConfig(), repo_root=tmp_path) if result else []

    assert result is not None
    symbols_by_name = {symbol.name: symbol for symbol in result.files[0].changed_symbols}
    references = symbols_by_name["supports"].references
    assert any(
        reference.file == "src/quote.module.ts" and reference.kind == "read" and reference.text == "@Module({"
        for reference in references
    )
    assert any(
        reference.file == "src/flow-handler.registry.ts" and "@Inject(FLOW_HANDLER_PORT)" in reference.text
        for reference in references
    )
    assert len(packs) == 1
    assert any("exports: [FlowHandlerRegistry]" in snippet.code for snippet in packs[0].reference_snippets)


def test_typescript_analyzer_collects_spread_provider_array_module_context(
    tmp_path: Path,
    built_ts_analyzer: None,
) -> None:
    src = tmp_path / "src"
    handlers = src / "handlers"
    handlers.mkdir(parents=True)
    (tmp_path / "package.json").write_text('{"name":"@acme/app","types":"./src/quote.module.ts"}', encoding="utf-8")
    (tmp_path / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler","strict":true,'
        '"experimentalDecorators":true},"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (src / "tokens.ts").write_text(
        "export const FLOW_HANDLER_PORT = Symbol('FLOW_HANDLER_PORT');\n",
        encoding="utf-8",
    )
    (src / "flow-handler.port.ts").write_text(
        "export interface FlowHandler {\n  supports(route: string): boolean;\n}\n",
        encoding="utf-8",
    )
    (handlers / "va-to-va-agent.handler.ts").write_text(
        "import type { FlowHandler } from '../flow-handler.port.js';\n"
        "export class VaToVaAgentHandler implements FlowHandler {\n"
        "  supports(route: string): boolean {\n"
        "    return true;\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    (src / "flow-handler.registry.ts").write_text(
        "import { FLOW_HANDLER_PORT } from './tokens.js';\n"
        "import type { FlowHandler } from './flow-handler.port.js';\n"
        "\n"
        "function Inject(_token: unknown): ParameterDecorator {\n"
        "  return () => undefined;\n"
        "}\n"
        "\n"
        "export class FlowHandlerRegistry {\n"
        "  constructor(@Inject(FLOW_HANDLER_PORT) private readonly handlers: ReadonlyArray<FlowHandler>) {}\n"
        "}\n",
        encoding="utf-8",
    )
    (src / "quote.module.ts").write_text(
        "import { VaToVaAgentHandler } from './handlers/va-to-va-agent.handler.js';\n"
        "import { FlowHandlerRegistry } from './flow-handler.registry.js';\n"
        "import { FLOW_HANDLER_PORT } from './tokens.js';\n"
        "\n"
        "function Module(_metadata: unknown): ClassDecorator {\n"
        "  return () => undefined;\n"
        "}\n"
        "\n"
        "const FLOW_HANDLER_PROVIDERS = [\n"
        "  {\n"
        "    provide: FLOW_HANDLER_PORT,\n"
        "    useFactory: (handler: VaToVaAgentHandler) => [handler],\n"
        "    inject: [VaToVaAgentHandler],\n"
        "  },\n"
        "];\n"
        "\n"
        "@Module({\n"
        "  providers: [\n"
        "    ...FLOW_HANDLER_PROVIDERS,\n"
        "    FlowHandlerRegistry,\n"
        "  ],\n"
        "  exports: [FlowHandlerRegistry],\n"
        "})\n"
        "export class QuoteModule {}\n",
        encoding="utf-8",
    )
    diff = parse_unified_diff(
        """diff --git a/src/handlers/va-to-va-agent.handler.ts b/src/handlers/va-to-va-agent.handler.ts
index 1111111..2222222 100644
--- a/src/handlers/va-to-va-agent.handler.ts
+++ b/src/handlers/va-to-va-agent.handler.ts
@@ -1,6 +1,6 @@
 import type { FlowHandler } from '../flow-handler.port.js';
 export class VaToVaAgentHandler implements FlowHandler {
   supports(route: string): boolean {
-    return route === 'VA_TO_VA';
+    return true;
   }
 }
""",
        TargetMode.PATCH,
    )
    diff = classify_diff(diff, ignore_patterns=[])

    result = run_typescript_analyzer(tmp_path, diff.files)
    packs = build_context_packs([result], diff.files, ReviewConfig(), repo_root=tmp_path) if result else []

    assert result is not None
    references = {symbol.name: symbol for symbol in result.files[0].changed_symbols}["supports"].references
    assert any(
        reference.file == "src/quote.module.ts" and reference.kind == "read" and reference.text == "@Module({"
        for reference in references
    )
    assert len(packs) == 1
    assert any("...FLOW_HANDLER_PROVIDERS" in snippet.code for snippet in packs[0].reference_snippets)


def test_typescript_analyzer_collects_provider_object_token_injection_references(
    built_ts_analyzer: None,
) -> None:
    fixture = TS_QUALITY_FIXTURE / "workspace_flow_handler_provider_omission"
    diff = parse_unified_diff((fixture / "change.diff").read_text(encoding="utf-8"), TargetMode.PATCH)
    diff = classify_diff(diff, ignore_patterns=[])

    result = run_typescript_analyzer(fixture / "repo", diff.files)
    packs = build_context_packs([result], diff.files, ReviewConfig(), repo_root=fixture / "repo") if result else []

    assert result is not None
    references = [reference for symbol in result.files[0].changed_symbols for reference in symbol.references]
    assert any(
        reference.file == "src/flow-handler.registry.ts" and "@Inject(FLOW_HANDLER_PORT)" in reference.text
        for reference in references
    )
    assert len(packs) == 1
    assert any("@Inject(FLOW_HANDLER_PORT)" in snippet.code for snippet in packs[0].reference_snippets)


def test_typescript_analyzer_collects_imported_provider_array_module_context(
    tmp_path: Path,
    built_ts_analyzer: None,
) -> None:
    src = tmp_path / "src"
    handlers = src / "handlers"
    handlers.mkdir(parents=True)
    (tmp_path / "package.json").write_text('{"name":"@acme/app","types":"./src/quote.module.ts"}', encoding="utf-8")
    (tmp_path / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler","strict":true,'
        '"experimentalDecorators":true},"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (src / "tokens.ts").write_text(
        "export const FLOW_HANDLER_PORT = Symbol('FLOW_HANDLER_PORT');\n",
        encoding="utf-8",
    )
    (src / "flow-handler.port.ts").write_text(
        "export interface FlowHandler {\n  supports(route: string): boolean;\n}\n",
        encoding="utf-8",
    )
    (handlers / "va-to-va-agent.handler.ts").write_text(
        "import type { FlowHandler } from '../flow-handler.port.js';\n"
        "export class VaToVaAgentHandler implements FlowHandler {\n"
        "  supports(route: string): boolean {\n"
        "    return true;\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    (src / "flow-handler.providers.ts").write_text(
        "import { VaToVaAgentHandler } from './handlers/va-to-va-agent.handler.js';\n"
        "import { FLOW_HANDLER_PORT } from './tokens.js';\n"
        "\n"
        "export const FLOW_HANDLER_PROVIDERS = [\n"
        "  {\n"
        "    provide: FLOW_HANDLER_PORT,\n"
        "    useFactory: (handler: VaToVaAgentHandler) => [handler],\n"
        "    inject: [VaToVaAgentHandler],\n"
        "  },\n"
        "];\n",
        encoding="utf-8",
    )
    (src / "flow-handler.registry.ts").write_text(
        "import { FLOW_HANDLER_PORT } from './tokens.js';\n"
        "import type { FlowHandler } from './flow-handler.port.js';\n"
        "\n"
        "function Inject(_token: unknown): ParameterDecorator {\n"
        "  return () => undefined;\n"
        "}\n"
        "\n"
        "export class FlowHandlerRegistry {\n"
        "  constructor(@Inject(FLOW_HANDLER_PORT) private readonly handlers: ReadonlyArray<FlowHandler>) {}\n"
        "}\n",
        encoding="utf-8",
    )
    (src / "quote.module.ts").write_text(
        "import { FLOW_HANDLER_PROVIDERS } from './flow-handler.providers.js';\n"
        "import { FlowHandlerRegistry } from './flow-handler.registry.js';\n"
        "\n"
        "function Module(_metadata: unknown): ClassDecorator {\n"
        "  return () => undefined;\n"
        "}\n"
        "\n"
        "@Module({\n"
        "  providers: [\n"
        "    ...FLOW_HANDLER_PROVIDERS,\n"
        "    FlowHandlerRegistry,\n"
        "  ],\n"
        "  exports: [FlowHandlerRegistry],\n"
        "})\n"
        "export class QuoteModule {}\n",
        encoding="utf-8",
    )
    diff = parse_unified_diff(
        """diff --git a/src/handlers/va-to-va-agent.handler.ts b/src/handlers/va-to-va-agent.handler.ts
index 1111111..2222222 100644
--- a/src/handlers/va-to-va-agent.handler.ts
+++ b/src/handlers/va-to-va-agent.handler.ts
@@ -1,6 +1,6 @@
 import type { FlowHandler } from '../flow-handler.port.js';
 export class VaToVaAgentHandler implements FlowHandler {
   supports(route: string): boolean {
-    return route === 'VA_TO_VA';
+    return true;
   }
 }
""",
        TargetMode.PATCH,
    )
    diff = classify_diff(diff, ignore_patterns=[])

    result = run_typescript_analyzer(tmp_path, diff.files)
    packs = build_context_packs([result], diff.files, ReviewConfig(), repo_root=tmp_path) if result else []

    assert result is not None
    references = {symbol.name: symbol for symbol in result.files[0].changed_symbols}["supports"].references
    assert any(
        reference.file == "src/quote.module.ts" and reference.kind == "read" and reference.text == "@Module({"
        for reference in references
    )
    assert len(packs) == 1
    assert any("...FLOW_HANDLER_PROVIDERS" in snippet.code for snippet in packs[0].reference_snippets)


def test_typescript_analyzer_collects_frozen_object_member_bracket_references(
    tmp_path: Path,
    built_ts_analyzer: None,
) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (tmp_path / "package.json").write_text('{"name":"@acme/app","types":"./src/webhook-routing.ts"}', encoding="utf-8")
    (tmp_path / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler","strict":true},'
        '"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (src / "webhook-routing.ts").write_text(
        "export const PROXY_TARGET_ENV = Object.freeze({\n"
        "  'identity-kyc': 'IDENTITY_KYC_URL',\n"
        "  vault: 'VAULT_URL',\n"
        "} as const);\n",
        encoding="utf-8",
    )
    (src / "webhook-routing.service.ts").write_text(
        "import { PROXY_TARGET_ENV } from './webhook-routing.js';\n"
        "\n"
        "export function envForTarget(target: 'identity-kyc' | 'vault'): string {\n"
        "  return PROXY_TARGET_ENV[target] ?? PROXY_TARGET_ENV['identity-kyc'];\n"
        "}\n",
        encoding="utf-8",
    )
    diff = parse_unified_diff(
        """diff --git a/src/webhook-routing.ts b/src/webhook-routing.ts
index 1111111..2222222 100644
--- a/src/webhook-routing.ts
+++ b/src/webhook-routing.ts
@@ -1,4 +1,4 @@
 export const PROXY_TARGET_ENV = Object.freeze({
-  'identity-kyc': 'IDENTITY_KYC_URL',
+  'identity-kyc': 'IDENTITY_TRAVEL_RULE_URL',
   vault: 'VAULT_URL',
 } as const);
""",
        TargetMode.PATCH,
    )
    diff = classify_diff(diff, ignore_patterns=[])

    result = run_typescript_analyzer(tmp_path, diff.files)
    packs = build_context_packs([result], diff.files, ReviewConfig(), repo_root=tmp_path) if result else []

    assert result is not None
    symbol = {symbol.name: symbol for symbol in result.files[0].changed_symbols}["identity-kyc"]
    assert any(
        reference.file == "src/webhook-routing.service.ts"
        and reference.kind == "read"
        and "PROXY_TARGET_ENV['identity-kyc']" in reference.text
        for reference in symbol.references
    )
    assert len(packs) == 1
    assert packs[0].id == "src/webhook-routing.ts#identity-kyc:1"
    assert any(
        snippet.file == "src/webhook-routing.service.ts" and "PROXY_TARGET_ENV['identity-kyc']" in snippet.code
        for snippet in packs[0].reference_snippets
    )


def test_typescript_analyzer_collects_satisfies_contract_for_object_member_change(
    tmp_path: Path,
    built_ts_analyzer: None,
) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (tmp_path / "package.json").write_text('{"name":"@acme/app","types":"./src/webhook-routing.ts"}', encoding="utf-8")
    (tmp_path / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler","strict":true},'
        '"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (src / "env.ts").write_text(
        "export interface Env {\n"
        "  COREBANK_IDENTITY_KYC_WEBHOOK_URL: string;\n"
        "  COREBANK_IDENTITY_TRAVEL_RULE_WEBHOOK_URL: string;\n"
        "}\n",
        encoding="utf-8",
    )
    (src / "webhook-routing.ts").write_text(
        "import type { Env } from './env.js';\n"
        "\n"
        "export const PROXY_TARGET_ENV = Object.freeze({\n"
        "  'identity-kyc': 'COREBANK_IDENTITY_TRAVEL_RULE_WEBHOOK_URL',\n"
        "} as const) satisfies Readonly<Record<string, keyof Env>>;\n",
        encoding="utf-8",
    )
    diff = parse_unified_diff(
        """diff --git a/src/webhook-routing.ts b/src/webhook-routing.ts
index 1111111..2222222 100644
--- a/src/webhook-routing.ts
+++ b/src/webhook-routing.ts
@@ -1,5 +1,5 @@
 import type { Env } from './env.js';

 export const PROXY_TARGET_ENV = Object.freeze({
-  'identity-kyc': 'COREBANK_IDENTITY_KYC_WEBHOOK_URL',
+  'identity-kyc': 'COREBANK_IDENTITY_TRAVEL_RULE_WEBHOOK_URL',
 } as const) satisfies Readonly<Record<string, keyof Env>>;
""",
        TargetMode.PATCH,
    )
    diff = classify_diff(diff, ignore_patterns=[])

    result = run_typescript_analyzer(tmp_path, diff.files)
    packs = build_context_packs([result], diff.files, ReviewConfig(), repo_root=tmp_path) if result else []

    assert result is not None
    symbol = {symbol.name: symbol for symbol in result.files[0].changed_symbols}["identity-kyc"]
    assert any(
        contract.file == "src/env.ts" and contract.kind == "contract" and "Env" in contract.text
        for contract in symbol.contracts
    )
    assert len(packs) == 1
    assert any(
        snippet.file == "src/env.ts" and "COREBANK_IDENTITY_KYC_WEBHOOK_URL" in snippet.code
        for snippet in packs[0].contract_snippets
    )


def test_typescript_analyzer_collects_interface_method_contract_for_class_method_change(
    tmp_path: Path,
    built_ts_analyzer: None,
) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (tmp_path / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler","strict":true},'
        '"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (src / "flow-handler.port.ts").write_text(
        "export interface FlowHandler {\n"
        "  /** Must return true only for routes this handler exclusively owns. */\n"
        "  supports(route: string): boolean;\n"
        "}\n",
        encoding="utf-8",
    )
    (src / "va-to-va-agent.handler.ts").write_text(
        "import type { FlowHandler } from './flow-handler.port.js';\n"
        "\n"
        "export class VaToVaAgentHandler implements FlowHandler {\n"
        "  supports(route: string): boolean {\n"
        "    return true;\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    (src / "flow-router.ts").write_text(
        "import type { FlowHandler } from './flow-handler.port.js';\n"
        "\n"
        "export function routeSupported(handler: FlowHandler, route: string): boolean {\n"
        "  return handler.supports(route);\n"
        "}\n",
        encoding="utf-8",
    )
    diff = parse_unified_diff(
        """diff --git a/src/va-to-va-agent.handler.ts b/src/va-to-va-agent.handler.ts
index 1111111..2222222 100644
--- a/src/va-to-va-agent.handler.ts
+++ b/src/va-to-va-agent.handler.ts
@@ -2,7 +2,7 @@ import type { FlowHandler } from './flow-handler.port.js';

 export class VaToVaAgentHandler implements FlowHandler {
   supports(route: string): boolean {
-    return route === 'VA_TO_VA';
+    return true;
   }
 }
""",
        TargetMode.PATCH,
    )
    diff = classify_diff(diff, ignore_patterns=[])

    result = run_typescript_analyzer(tmp_path, diff.files)
    packs = build_context_packs([result], diff.files, ReviewConfig(), repo_root=tmp_path) if result else []

    assert result is not None
    symbol = {symbol.name: symbol for symbol in result.files[0].changed_symbols}["supports"]
    assert any(
        contract.file == "src/flow-handler.port.ts"
        and contract.kind == "contract"
        and "supports(route: string): boolean;" in contract.text
        for contract in symbol.contracts
    )
    assert any(
        reference.file == "src/flow-router.ts"
        and reference.kind == "call"
        and "handler.supports(route)" in reference.text
        for reference in symbol.references
    )
    assert len(packs) == 1
    assert any(
        snippet.file == "src/flow-handler.port.ts"
        and "Must return true only for routes this handler exclusively owns" in snippet.code
        for snippet in packs[0].contract_snippets
    )
    assert any("Contract context:" in note for note in packs[0].impact_notes)


def test_typescript_analyzer_prioritizes_source_references_over_test_calls(
    tmp_path: Path,
    built_ts_analyzer: None,
) -> None:
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (tmp_path / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Node"},"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (src_dir / "settings.ts").write_text(
        "export function tenantScopedSettingsKey(tenantId: string, userId: string): string {\n  return userId;\n}\n",
        encoding="utf-8",
    )
    (src_dir / "a-settings.test.ts").write_text(
        'import { tenantScopedSettingsKey } from "./settings";\n'
        + "".join(
            f"expect(tenantScopedSettingsKey('tenant-test-{index}', 'user-a')).toBe('user-a');\n" for index in range(8)
        ),
        encoding="utf-8",
    )
    (src_dir / "z-dashboard.ts").write_text(
        'import { tenantScopedSettingsKey } from "./settings";\n'
        "export const dashboardKey = tenantScopedSettingsKey('tenant-source', 'user-a');\n",
        encoding="utf-8",
    )
    diff = parse_unified_diff(
        """diff --git a/src/settings.ts b/src/settings.ts
index 1111111..2222222 100644
--- a/src/settings.ts
+++ b/src/settings.ts
@@ -1,3 +1,3 @@
 export function tenantScopedSettingsKey(tenantId: string, userId: string): string {
-  return `${tenantId}:${userId}`;
+  return userId;
 }
""",
        TargetMode.PATCH,
    )
    diff = classify_diff(diff, ignore_patterns=[])

    result = run_typescript_analyzer(tmp_path, diff.files)

    assert result is not None
    references = result.files[0].changed_symbols[0].references
    call_references = [reference for reference in references if reference.kind == "call"]
    assert call_references[0].file == "src/z-dashboard.ts"


def test_typescript_analyzer_ignores_same_name_unrelated_symbols(tmp_path: Path, built_ts_analyzer: None) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Node"},"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (tmp_path / "src" / "money.ts").write_text(
        "export function formatAmount(value: number): string {\n  return `${value}`;\n}\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "checkout.ts").write_text(
        'import { formatAmount } from "./money";\nexport const label = formatAmount(10);\n',
        encoding="utf-8",
    )
    (tmp_path / "src" / "unrelated.ts").write_text(
        "export function formatAmount(value: number): number {\n"
        "  return value;\n"
        "}\n"
        "export const local = formatAmount(1);\n",
        encoding="utf-8",
    )
    diff = parse_unified_diff(
        """diff --git a/src/money.ts b/src/money.ts
index 1111111..2222222 100644
--- a/src/money.ts
+++ b/src/money.ts
@@ -1,3 +1,3 @@
 export function formatAmount(value: number): string {
-  return `$${value}`;
+  return `${value}`;
 }
""",
        TargetMode.PATCH,
    )
    diff = classify_diff(diff, ignore_patterns=[])

    result = run_typescript_analyzer(tmp_path, diff.files)

    assert result is not None
    references = result.files[0].changed_symbols[0].references
    assert any(reference.file == "src/checkout.ts" and reference.kind == "call" for reference in references)
    assert all(reference.file != "src/unrelated.ts" for reference in references)


def test_typescript_analyzer_uses_nearest_workspace_tsconfig_for_alias_references(
    tmp_path: Path,
    built_ts_analyzer: None,
) -> None:
    app_src = tmp_path / "apps" / "api" / "src"
    app_src.mkdir(parents=True)
    (tmp_path / "package.json").write_text('{"workspaces":["apps/*"]}', encoding="utf-8")
    (tmp_path / "apps" / "api" / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler",'
        '"baseUrl":".","paths":{"@/*":["src/*"]}},"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (app_src / "settings.ts").write_text(
        "export function workspaceCacheKey(tenantId: string, userId: string): string {\n  return userId;\n}\n",
        encoding="utf-8",
    )
    (app_src / "service.ts").write_text(
        'import { workspaceCacheKey } from "@/settings";\n'
        "export const key = workspaceCacheKey('tenant-a', 'user-a');\n",
        encoding="utf-8",
    )
    diff = parse_unified_diff(
        """diff --git a/apps/api/src/settings.ts b/apps/api/src/settings.ts
index 1111111..2222222 100644
--- a/apps/api/src/settings.ts
+++ b/apps/api/src/settings.ts
@@ -1,3 +1,3 @@
 export function workspaceCacheKey(tenantId: string, userId: string): string {
-  return `${tenantId}:${userId}`;
+  return userId;
 }
""",
        TargetMode.PATCH,
    )
    diff = classify_diff(diff, ignore_patterns=[])

    result = run_typescript_analyzer(tmp_path, diff.files)

    assert result is not None
    assert result.files[0].tsconfig_path == str(tmp_path / "apps" / "api" / "tsconfig.json")
    references = result.files[0].changed_symbols[0].references
    assert any(
        reference.file == "apps/api/src/service.ts"
        and reference.kind == "call"
        and "workspaceCacheKey('tenant-a', 'user-a')" in reference.text
        for reference in references
    )


def test_typescript_analyzer_resolves_tsconfig_path_alias_references_across_packages(
    tmp_path: Path,
    built_ts_analyzer: None,
) -> None:
    package_src = tmp_path / "packages" / "settings" / "src"
    app_src = tmp_path / "apps" / "api" / "src"
    app_tests = tmp_path / "apps" / "api" / "test"
    package_src.mkdir(parents=True)
    app_src.mkdir(parents=True)
    app_tests.mkdir(parents=True)
    (tmp_path / "package.json").write_text('{"workspaces":["apps/*","packages/*"]}', encoding="utf-8")
    (tmp_path / "packages" / "settings" / "package.json").write_text(
        '{"name":"@acme/settings","types":"./src/index.ts"}',
        encoding="utf-8",
    )
    (tmp_path / "packages" / "settings" / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler","strict":true},'
        '"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (tmp_path / "apps" / "api" / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler",'
        '"baseUrl":".","paths":{"@settings/*":["../../packages/settings/src/*"]},'
        '"strict":true},"include":["src/**/*.ts","test/**/*.ts"]}',
        encoding="utf-8",
    )
    (package_src / "cache.ts").write_text(
        "export function workspaceCacheKey(tenantId: string, userId: string): string {\n  return userId;\n}\n",
        encoding="utf-8",
    )
    (app_src / "service.ts").write_text(
        'import { workspaceCacheKey } from "@settings/cache";\n'
        "export const key = workspaceCacheKey('tenant-a', 'user-a');\n",
        encoding="utf-8",
    )
    (app_tests / "tenant-settings.test.ts").write_text(
        'import { workspaceCacheKey } from "@settings/cache";\n'
        "expect(workspaceCacheKey('tenant-a', 'user-a')).toBe('tenant-a:user-a');\n",
        encoding="utf-8",
    )
    diff = parse_unified_diff(
        """diff --git a/packages/settings/src/cache.ts b/packages/settings/src/cache.ts
index 1111111..2222222 100644
--- a/packages/settings/src/cache.ts
+++ b/packages/settings/src/cache.ts
@@ -1,3 +1,3 @@
 export function workspaceCacheKey(tenantId: string, userId: string): string {
-  return `${tenantId}:${userId}`;
+  return userId;
 }
""",
        TargetMode.PATCH,
    )
    diff = classify_diff(diff, ignore_patterns=[])

    result = run_typescript_analyzer(tmp_path, diff.files)

    assert result is not None
    references = result.files[0].changed_symbols[0].references
    assert any(
        reference.file == "apps/api/src/service.ts"
        and reference.kind == "call"
        and "workspaceCacheKey('tenant-a', 'user-a')" in reference.text
        for reference in references
    )
    assert "apps/api/test/tenant-settings.test.ts" in result.files[0].related_tests


def test_typescript_analyzer_resolves_path_aliases_from_workspace_tsconfig_extends(
    tmp_path: Path,
    built_ts_analyzer: None,
) -> None:
    config_pkg = tmp_path / "packages" / "tsconfig"
    package_src = tmp_path / "packages" / "settings" / "src"
    app_src = tmp_path / "apps" / "api" / "src"
    config_pkg.mkdir(parents=True)
    package_src.mkdir(parents=True)
    app_src.mkdir(parents=True)
    (tmp_path / "package.json").write_text('{"workspaces":["apps/*","packages/*"]}', encoding="utf-8")
    (config_pkg / "package.json").write_text('{"name":"@acme/tsconfig","version":"0.0.0"}', encoding="utf-8")
    (config_pkg / "node.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler",'
        '"baseUrl":"../..","paths":{"@settings/*":["packages/settings/src/*"]}}}',
        encoding="utf-8",
    )
    (tmp_path / "packages" / "settings" / "package.json").write_text(
        '{"name":"@acme/settings","types":"./src/cache.ts"}',
        encoding="utf-8",
    )
    (tmp_path / "packages" / "settings" / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler"},'
        '"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (tmp_path / "apps" / "api" / "tsconfig.json").write_text(
        '{"extends":"@acme/tsconfig/node.json","include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (package_src / "cache.ts").write_text(
        "export function workspaceCacheKey(tenantId: string, userId: string): string {\n  return userId;\n}\n",
        encoding="utf-8",
    )
    (app_src / "service.ts").write_text(
        'import { workspaceCacheKey } from "@settings/cache";\n'
        "export const key = workspaceCacheKey('tenant-a', 'user-a');\n",
        encoding="utf-8",
    )
    diff = parse_unified_diff(
        """diff --git a/packages/settings/src/cache.ts b/packages/settings/src/cache.ts
index 1111111..2222222 100644
--- a/packages/settings/src/cache.ts
+++ b/packages/settings/src/cache.ts
@@ -1,3 +1,3 @@
 export function workspaceCacheKey(tenantId: string, userId: string): string {
-  return `${tenantId}:${userId}`;
+  return userId;
 }
""",
        TargetMode.PATCH,
    )
    diff = classify_diff(diff, ignore_patterns=[])

    result = run_typescript_analyzer(tmp_path, diff.files)

    assert result is not None
    references = result.files[0].changed_symbols[0].references
    assert any(
        reference.file == "apps/api/src/service.ts"
        and reference.kind == "call"
        and "workspaceCacheKey('tenant-a', 'user-a')" in reference.text
        for reference in references
    )


def test_typescript_analyzer_collects_workspace_package_import_references(
    tmp_path: Path,
    built_ts_analyzer: None,
) -> None:
    package_src = tmp_path / "packages" / "settings" / "src"
    app_src = tmp_path / "apps" / "api" / "src"
    package_src.mkdir(parents=True)
    app_src.mkdir(parents=True)
    (tmp_path / "package.json").write_text('{"workspaces":["apps/*","packages/*"]}', encoding="utf-8")
    (tmp_path / "packages" / "settings" / "package.json").write_text(
        '{"name":"@acme/settings","types":"./src/index.ts"}',
        encoding="utf-8",
    )
    (tmp_path / "packages" / "settings" / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler","strict":true},'
        '"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (tmp_path / "apps" / "api" / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler","strict":true},'
        '"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (package_src / "index.ts").write_text(
        "export function tenantScopedSettingsKey(tenantId: string, userId: string): string {\n  return userId;\n}\n",
        encoding="utf-8",
    )
    (app_src / "service.ts").write_text(
        'import { tenantScopedSettingsKey } from "@acme/settings";\n'
        "export const key = tenantScopedSettingsKey('tenant-a', 'user-a');\n",
        encoding="utf-8",
    )
    diff = parse_unified_diff(
        """diff --git a/packages/settings/src/index.ts b/packages/settings/src/index.ts
index 1111111..2222222 100644
--- a/packages/settings/src/index.ts
+++ b/packages/settings/src/index.ts
@@ -1,3 +1,3 @@
 export function tenantScopedSettingsKey(tenantId: string, userId: string): string {
-  return `${tenantId}:${userId}`;
+  return userId;
 }
""",
        TargetMode.PATCH,
    )
    diff = classify_diff(diff, ignore_patterns=[])

    result = run_typescript_analyzer(tmp_path, diff.files)

    assert result is not None
    references = result.files[0].changed_symbols[0].references
    assert any(
        reference.file == "apps/api/src/service.ts"
        and reference.kind == "call"
        and "tenantScopedSettingsKey('tenant-a', 'user-a')" in reference.text
        for reference in references
    )


def test_typescript_analyzer_resolves_workspace_package_types_entrypoint(
    tmp_path: Path,
    built_ts_analyzer: None,
) -> None:
    package_src = tmp_path / "packages" / "settings" / "src"
    app_src = tmp_path / "apps" / "api" / "src"
    package_src.mkdir(parents=True)
    app_src.mkdir(parents=True)
    (tmp_path / "package.json").write_text('{"workspaces":["apps/*","packages/*"]}', encoding="utf-8")
    (tmp_path / "packages" / "settings" / "package.json").write_text(
        '{"name":"@acme/settings","types":"./src/public-api.ts","main":"./dist/public-api.js"}',
        encoding="utf-8",
    )
    (tmp_path / "packages" / "settings" / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler","strict":true},'
        '"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (tmp_path / "apps" / "api" / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler","strict":true},'
        '"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (package_src / "public-api.ts").write_text(
        "export function packageEntrypointSettingsKey(tenantId: string, userId: string): string {\n"
        "  return userId;\n"
        "}\n",
        encoding="utf-8",
    )
    (app_src / "service.ts").write_text(
        'import { packageEntrypointSettingsKey } from "@acme/settings";\n'
        "export const key = packageEntrypointSettingsKey('tenant-a', 'user-a');\n",
        encoding="utf-8",
    )
    diff = parse_unified_diff(
        """diff --git a/packages/settings/src/public-api.ts b/packages/settings/src/public-api.ts
index 1111111..2222222 100644
--- a/packages/settings/src/public-api.ts
+++ b/packages/settings/src/public-api.ts
@@ -1,3 +1,3 @@
 export function packageEntrypointSettingsKey(tenantId: string, userId: string): string {
-  return `${tenantId}:${userId}`;
+  return userId;
 }
""",
        TargetMode.PATCH,
    )
    diff = classify_diff(diff, ignore_patterns=[])

    result = run_typescript_analyzer(tmp_path, diff.files)

    assert result is not None
    references = result.files[0].changed_symbols[0].references
    assert any(
        reference.file == "apps/api/src/service.ts"
        and reference.kind == "call"
        and "packageEntrypointSettingsKey('tenant-a', 'user-a')" in reference.text
        for reference in references
    )


def test_typescript_analyzer_does_not_treat_bare_package_import_as_any_package_file(
    tmp_path: Path,
    built_ts_analyzer: None,
) -> None:
    package_src = tmp_path / "packages" / "settings" / "src"
    app_tests = tmp_path / "apps" / "api" / "test"
    package_src.mkdir(parents=True)
    app_tests.mkdir(parents=True)
    (tmp_path / "package.json").write_text('{"workspaces":["apps/*","packages/*"]}', encoding="utf-8")
    (tmp_path / "packages" / "settings" / "package.json").write_text(
        '{"name":"@acme/settings","types":"./src/public-api.ts"}',
        encoding="utf-8",
    )
    (tmp_path / "packages" / "settings" / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler","strict":true},'
        '"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (package_src / "public-api.ts").write_text(
        "export function publicSettingsKey(tenantId: string, userId: string): string {\n"
        "  return `${tenantId}:${userId}`;\n"
        "}\n",
        encoding="utf-8",
    )
    (package_src / "internal.ts").write_text(
        "export function privateSettingsKey(tenantId: string, userId: string): string {\n  return userId;\n}\n",
        encoding="utf-8",
    )
    (app_tests / "settings.test.ts").write_text(
        'import { publicSettingsKey } from "@acme/settings";\n'
        "expect(publicSettingsKey('tenant-a', 'user-a')).toBe('tenant-a:user-a');\n",
        encoding="utf-8",
    )
    diff = parse_unified_diff(
        """diff --git a/packages/settings/src/internal.ts b/packages/settings/src/internal.ts
index 1111111..2222222 100644
--- a/packages/settings/src/internal.ts
+++ b/packages/settings/src/internal.ts
@@ -1,3 +1,3 @@
 export function privateSettingsKey(tenantId: string, userId: string): string {
-  return `${tenantId}:${userId}`;
+  return userId;
 }
""",
        TargetMode.PATCH,
    )
    diff = classify_diff(diff, ignore_patterns=[])

    result = run_typescript_analyzer(tmp_path, diff.files)

    assert result is not None
    assert result.files[0].related_tests == []


def test_typescript_analyzer_collects_workspace_package_default_import_references(
    tmp_path: Path,
    built_ts_analyzer: None,
) -> None:
    package_src = tmp_path / "packages" / "settings" / "src"
    app_src = tmp_path / "apps" / "api" / "src"
    package_src.mkdir(parents=True)
    app_src.mkdir(parents=True)
    (tmp_path / "package.json").write_text('{"workspaces":["apps/*","packages/*"]}', encoding="utf-8")
    (tmp_path / "packages" / "settings" / "package.json").write_text(
        '{"name":"@acme/settings","types":"./src/index.ts"}',
        encoding="utf-8",
    )
    (tmp_path / "packages" / "settings" / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler","strict":true},'
        '"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (tmp_path / "apps" / "api" / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler","strict":true},'
        '"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (package_src / "index.ts").write_text(
        "export default function tenantScopedSettingsKey(tenantId: string, userId: string): string {\n"
        "  return userId;\n"
        "}\n",
        encoding="utf-8",
    )
    (app_src / "service.ts").write_text(
        'import tenantSettingsKey from "@acme/settings";\n'
        "export const key = tenantSettingsKey('tenant-a', 'user-a');\n",
        encoding="utf-8",
    )
    diff = parse_unified_diff(
        """diff --git a/packages/settings/src/index.ts b/packages/settings/src/index.ts
index 1111111..2222222 100644
--- a/packages/settings/src/index.ts
+++ b/packages/settings/src/index.ts
@@ -1,3 +1,3 @@
 export default function tenantScopedSettingsKey(tenantId: string, userId: string): string {
-  return `${tenantId}:${userId}`;
+  return userId;
 }
""",
        TargetMode.PATCH,
    )
    diff = classify_diff(diff, ignore_patterns=[])

    result = run_typescript_analyzer(tmp_path, diff.files)

    assert result is not None
    references = result.files[0].changed_symbols[0].references
    assert any(
        reference.file == "apps/api/src/service.ts"
        and reference.kind == "call"
        and "tenantSettingsKey('tenant-a', 'user-a')" in reference.text
        for reference in references
    )


def test_typescript_analyzer_collects_workspace_package_barrel_alias_references(
    tmp_path: Path,
    built_ts_analyzer: None,
) -> None:
    package_src = tmp_path / "packages" / "settings" / "src"
    app_src = tmp_path / "apps" / "api" / "src"
    package_src.mkdir(parents=True)
    app_src.mkdir(parents=True)
    (tmp_path / "package.json").write_text('{"workspaces":["apps/*","packages/*"]}', encoding="utf-8")
    (tmp_path / "packages" / "settings" / "package.json").write_text(
        '{"name":"@acme/settings","types":"./src/index.ts"}',
        encoding="utf-8",
    )
    (tmp_path / "packages" / "settings" / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler","strict":true},'
        '"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (tmp_path / "apps" / "api" / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler","strict":true},'
        '"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (package_src / "internal.ts").write_text(
        "export function buildTenantSettingsKey(tenantId: string, userId: string): string {\n  return userId;\n}\n",
        encoding="utf-8",
    )
    (package_src / "index.ts").write_text(
        'export { tenantScopedSettingsKey as publicTenantSettingsKey } from "./public";\n',
        encoding="utf-8",
    )
    (package_src / "public.ts").write_text(
        'export { buildTenantSettingsKey as tenantScopedSettingsKey } from "./internal";\n',
        encoding="utf-8",
    )
    (app_src / "service.ts").write_text(
        'import { publicTenantSettingsKey } from "@acme/settings";\n'
        "export const key = publicTenantSettingsKey('tenant-a', 'user-a');\n",
        encoding="utf-8",
    )
    diff = parse_unified_diff(
        """diff --git a/packages/settings/src/internal.ts b/packages/settings/src/internal.ts
index 1111111..2222222 100644
--- a/packages/settings/src/internal.ts
+++ b/packages/settings/src/internal.ts
@@ -1,3 +1,3 @@
 export function buildTenantSettingsKey(tenantId: string, userId: string): string {
-  return `${tenantId}:${userId}`;
+  return userId;
 }
""",
        TargetMode.PATCH,
    )
    diff = classify_diff(diff, ignore_patterns=[])

    result = run_typescript_analyzer(tmp_path, diff.files)

    assert result is not None
    references = result.files[0].changed_symbols[0].references
    assert any(
        reference.file == "apps/api/src/service.ts"
        and reference.kind == "call"
        and "publicTenantSettingsKey('tenant-a', 'user-a')" in reference.text
        for reference in references
    )


def test_typescript_analyzer_collects_relative_barrel_alias_references_across_packages(
    tmp_path: Path,
    built_ts_analyzer: None,
) -> None:
    package_src = tmp_path / "packages" / "settings" / "src"
    app_src = tmp_path / "apps" / "api" / "src"
    package_src.mkdir(parents=True)
    app_src.mkdir(parents=True)
    (tmp_path / "package.json").write_text('{"workspaces":["apps/*","packages/*"]}', encoding="utf-8")
    (tmp_path / "packages" / "settings" / "package.json").write_text(
        '{"name":"@acme/settings","types":"./src/index.ts"}',
        encoding="utf-8",
    )
    (tmp_path / "packages" / "settings" / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler","strict":true},'
        '"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (tmp_path / "apps" / "api" / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler","strict":true},'
        '"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (package_src / "internal.ts").write_text(
        "export function buildTenantSettingsKey(tenantId: string, userId: string): string {\n  return userId;\n}\n",
        encoding="utf-8",
    )
    (package_src / "index.ts").write_text(
        'export { buildTenantSettingsKey as tenantScopedSettingsKey } from "./internal";\n',
        encoding="utf-8",
    )
    (app_src / "service.ts").write_text(
        'import { tenantScopedSettingsKey } from "../../../packages/settings/src";\n'
        "export const key = tenantScopedSettingsKey('tenant-a', 'user-a');\n",
        encoding="utf-8",
    )
    diff = parse_unified_diff(
        """diff --git a/packages/settings/src/internal.ts b/packages/settings/src/internal.ts
index 1111111..2222222 100644
--- a/packages/settings/src/internal.ts
+++ b/packages/settings/src/internal.ts
@@ -1,3 +1,3 @@
 export function buildTenantSettingsKey(tenantId: string, userId: string): string {
-  return `${tenantId}:${userId}`;
+  return userId;
 }
""",
        TargetMode.PATCH,
    )
    diff = classify_diff(diff, ignore_patterns=[])

    result = run_typescript_analyzer(tmp_path, diff.files)

    assert result is not None
    references = result.files[0].changed_symbols[0].references
    assert any(
        reference.file == "apps/api/src/service.ts"
        and reference.kind == "call"
        and "tenantScopedSettingsKey('tenant-a', 'user-a')" in reference.text
        for reference in references
    )


def test_typescript_analyzer_collects_relative_star_reexport_references_across_packages(
    tmp_path: Path,
    built_ts_analyzer: None,
) -> None:
    package_src = tmp_path / "packages" / "settings" / "src"
    app_src = tmp_path / "apps" / "api" / "src"
    package_src.mkdir(parents=True)
    app_src.mkdir(parents=True)
    (tmp_path / "package.json").write_text('{"workspaces":["apps/*","packages/*"]}', encoding="utf-8")
    (tmp_path / "packages" / "settings" / "package.json").write_text(
        '{"name":"@acme/settings","types":"./src/index.ts"}',
        encoding="utf-8",
    )
    (tmp_path / "packages" / "settings" / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler","strict":true},'
        '"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (tmp_path / "apps" / "api" / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler","strict":true},'
        '"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (package_src / "internal.ts").write_text(
        "export function buildTenantSettingsKey(tenantId: string, userId: string): string {\n  return userId;\n}\n",
        encoding="utf-8",
    )
    (package_src / "index.ts").write_text(
        'export * from "./internal";\n',
        encoding="utf-8",
    )
    (app_src / "service.ts").write_text(
        'import { buildTenantSettingsKey } from "../../../packages/settings/src";\n'
        "export const key = buildTenantSettingsKey('tenant-a', 'user-a');\n",
        encoding="utf-8",
    )
    diff = parse_unified_diff(
        """diff --git a/packages/settings/src/internal.ts b/packages/settings/src/internal.ts
index 1111111..2222222 100644
--- a/packages/settings/src/internal.ts
+++ b/packages/settings/src/internal.ts
@@ -1,3 +1,3 @@
 export function buildTenantSettingsKey(tenantId: string, userId: string): string {
-  return `${tenantId}:${userId}`;
+  return userId;
 }
""",
        TargetMode.PATCH,
    )
    diff = classify_diff(diff, ignore_patterns=[])

    result = run_typescript_analyzer(tmp_path, diff.files)

    assert result is not None
    references = result.files[0].changed_symbols[0].references
    assert any(
        reference.file == "apps/api/src/service.ts"
        and reference.kind == "call"
        and "buildTenantSettingsKey('tenant-a', 'user-a')" in reference.text
        for reference in references
    )


def test_typescript_analyzer_collects_workspace_package_namespace_reexport_references(
    tmp_path: Path,
    built_ts_analyzer: None,
) -> None:
    package_src = tmp_path / "packages" / "settings" / "src"
    app_src = tmp_path / "apps" / "api" / "src"
    package_src.mkdir(parents=True)
    app_src.mkdir(parents=True)
    (tmp_path / "package.json").write_text('{"workspaces":["apps/*","packages/*"]}', encoding="utf-8")
    (tmp_path / "packages" / "settings" / "package.json").write_text(
        '{"name":"@acme/settings","types":"./src/index.ts"}',
        encoding="utf-8",
    )
    (tmp_path / "packages" / "settings" / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler","strict":true},'
        '"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (tmp_path / "apps" / "api" / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler","strict":true},'
        '"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (package_src / "internal.ts").write_text(
        "export function buildTenantSettingsKey(tenantId: string, userId: string): string {\n  return userId;\n}\n",
        encoding="utf-8",
    )
    (package_src / "index.ts").write_text(
        'export * as settingsKeys from "./internal";\n',
        encoding="utf-8",
    )
    (app_src / "service.ts").write_text(
        'import { settingsKeys } from "@acme/settings";\n'
        "export const key = settingsKeys.buildTenantSettingsKey('tenant-a', 'user-a');\n",
        encoding="utf-8",
    )
    diff = parse_unified_diff(
        """diff --git a/packages/settings/src/internal.ts b/packages/settings/src/internal.ts
index 1111111..2222222 100644
--- a/packages/settings/src/internal.ts
+++ b/packages/settings/src/internal.ts
@@ -1,3 +1,3 @@
 export function buildTenantSettingsKey(tenantId: string, userId: string): string {
-  return `${tenantId}:${userId}`;
+  return userId;
 }
""",
        TargetMode.PATCH,
    )
    diff = classify_diff(diff, ignore_patterns=[])

    result = run_typescript_analyzer(tmp_path, diff.files)

    assert result is not None
    references = result.files[0].changed_symbols[0].references
    assert any(
        reference.file == "apps/api/src/service.ts"
        and reference.kind == "call"
        and "settingsKeys.buildTenantSettingsKey('tenant-a', 'user-a')" in reference.text
        for reference in references
    )


def test_typescript_analyzer_collects_namespace_import_namespace_reexport_references(
    tmp_path: Path,
    built_ts_analyzer: None,
) -> None:
    package_src = tmp_path / "packages" / "settings" / "src"
    app_src = tmp_path / "apps" / "api" / "src"
    package_src.mkdir(parents=True)
    app_src.mkdir(parents=True)
    (tmp_path / "package.json").write_text('{"workspaces":["apps/*","packages/*"]}', encoding="utf-8")
    (tmp_path / "packages" / "settings" / "package.json").write_text(
        '{"name":"@acme/settings","types":"./src/index.ts"}',
        encoding="utf-8",
    )
    (tmp_path / "packages" / "settings" / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler","strict":true},'
        '"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (tmp_path / "apps" / "api" / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler","strict":true},'
        '"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (package_src / "internal.ts").write_text(
        "export function buildTenantSettingsKey(tenantId: string, userId: string): string {\n  return userId;\n}\n",
        encoding="utf-8",
    )
    (package_src / "index.ts").write_text(
        'export * as settingsKeys from "./internal";\n',
        encoding="utf-8",
    )
    (app_src / "service.ts").write_text(
        'import * as settings from "@acme/settings";\n'
        "export const key = settings.settingsKeys.buildTenantSettingsKey('tenant-a', 'user-a');\n",
        encoding="utf-8",
    )
    diff = parse_unified_diff(
        """diff --git a/packages/settings/src/internal.ts b/packages/settings/src/internal.ts
index 1111111..2222222 100644
--- a/packages/settings/src/internal.ts
+++ b/packages/settings/src/internal.ts
@@ -1,3 +1,3 @@
 export function buildTenantSettingsKey(tenantId: string, userId: string): string {
-  return `${tenantId}:${userId}`;
+  return userId;
 }
""",
        TargetMode.PATCH,
    )
    diff = classify_diff(diff, ignore_patterns=[])

    result = run_typescript_analyzer(tmp_path, diff.files)

    assert result is not None
    references = result.files[0].changed_symbols[0].references
    assert any(
        reference.file == "apps/api/src/service.ts"
        and reference.kind == "call"
        and "settings.settingsKeys.buildTenantSettingsKey('tenant-a', 'user-a')" in reference.text
        for reference in references
    )


def test_typescript_analyzer_collects_exported_const_object_member_references(
    tmp_path: Path,
    built_ts_analyzer: None,
) -> None:
    package_src = tmp_path / "packages" / "types" / "src"
    app_src = tmp_path / "apps" / "api" / "src"
    package_src.mkdir(parents=True)
    app_src.mkdir(parents=True)
    (tmp_path / "package.json").write_text('{"workspaces":["apps/*","packages/*"]}', encoding="utf-8")
    (tmp_path / "packages" / "types" / "package.json").write_text(
        '{"name":"@acme/types","types":"./src/index.ts"}',
        encoding="utf-8",
    )
    (tmp_path / "packages" / "types" / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler","strict":true},'
        '"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (tmp_path / "apps" / "api" / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler","strict":true},'
        '"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (package_src / "permission.ts").write_text(
        "export const Permission = {\n"
        '  WEBHOOK_INBOX_VIEW: "WEBHOOK_INBOX_VIEW",\n'
        '  WEBHOOK_RETRIGGER: "WEBHOOK_RETRIGGER_DISABLED",\n'
        "} as const;\n",
        encoding="utf-8",
    )
    (package_src / "index.ts").write_text(
        'export { Permission } from "./permission";\n',
        encoding="utf-8",
    )
    (app_src / "admin-webhook.controller.ts").write_text(
        'import { Permission } from "@acme/types";\n'
        "\n"
        "export const retriggerPermission = Permission.WEBHOOK_RETRIGGER;\n",
        encoding="utf-8",
    )
    diff = parse_unified_diff(
        """diff --git a/packages/types/src/permission.ts b/packages/types/src/permission.ts
index 1111111..2222222 100644
--- a/packages/types/src/permission.ts
+++ b/packages/types/src/permission.ts
@@ -1,4 +1,4 @@
 export const Permission = {
   WEBHOOK_INBOX_VIEW: "WEBHOOK_INBOX_VIEW",
-  WEBHOOK_RETRIGGER: "WEBHOOK_RETRIGGER",
+  WEBHOOK_RETRIGGER: "WEBHOOK_RETRIGGER_DISABLED",
 } as const;
""",
        TargetMode.PATCH,
    )
    diff = classify_diff(diff, ignore_patterns=[])

    result = run_typescript_analyzer(tmp_path, diff.files)

    assert result is not None
    symbols_by_name = {symbol.name: symbol for symbol in result.files[0].changed_symbols}
    references = symbols_by_name["WEBHOOK_RETRIGGER"].references
    assert any(
        reference.file == "apps/api/src/admin-webhook.controller.ts"
        and reference.kind == "read"
        and "Permission.WEBHOOK_RETRIGGER" in reference.text
        for reference in references
    )


def test_typescript_analyzer_collects_workspace_enum_member_references(
    tmp_path: Path,
    built_ts_analyzer: None,
) -> None:
    package_src = tmp_path / "packages" / "types" / "src"
    app_src = tmp_path / "apps" / "api" / "src"
    package_src.mkdir(parents=True)
    app_src.mkdir(parents=True)
    (tmp_path / "package.json").write_text('{"workspaces":["apps/*","packages/*"]}', encoding="utf-8")
    (tmp_path / "packages" / "types" / "package.json").write_text(
        '{"name":"@acme/types","types":"./src/index.ts"}',
        encoding="utf-8",
    )
    (tmp_path / "packages" / "types" / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler","strict":true},'
        '"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (tmp_path / "apps" / "api" / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler","strict":true},'
        '"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (package_src / "permission.ts").write_text(
        "export enum Permission {\n"
        "  WEBHOOK_INBOX_VIEW = 'WEBHOOK_INBOX_VIEW',\n"
        "  WEBHOOK_RETRIGGER = 'WEBHOOK_RETRIGGER_DISABLED',\n"
        "}\n"
        "\n"
        "export const ROLE_PERMISSIONS = {\n"
        "  ADMIN: [Permission.WEBHOOK_INBOX_VIEW, Permission.WEBHOOK_RETRIGGER],\n"
        "  AUDITOR: [Permission.WEBHOOK_INBOX_VIEW],\n"
        "} as const;\n",
        encoding="utf-8",
    )
    (package_src / "index.ts").write_text(
        'export { Permission, ROLE_PERMISSIONS } from "./permission";\n',
        encoding="utf-8",
    )
    (app_src / "admin-webhook.controller.ts").write_text(
        'import { Permission } from "@acme/types";\n'
        "\n"
        "export const retriggerPermission = Permission.WEBHOOK_RETRIGGER;\n",
        encoding="utf-8",
    )
    (app_src / "admin-webhook.controller.test.ts").write_text(
        'import { Permission, ROLE_PERMISSIONS } from "@acme/types";\n'
        "\n"
        "it('allows admins to retrigger webhooks', () => {\n"
        "  expect(ROLE_PERMISSIONS.ADMIN).toContain(Permission.WEBHOOK_RETRIGGER);\n"
        "});\n",
        encoding="utf-8",
    )
    diff = parse_unified_diff(
        """diff --git a/packages/types/src/permission.ts b/packages/types/src/permission.ts
index 1111111..2222222 100644
--- a/packages/types/src/permission.ts
+++ b/packages/types/src/permission.ts
@@ -1,5 +1,5 @@
 export enum Permission {
   WEBHOOK_INBOX_VIEW = 'WEBHOOK_INBOX_VIEW',
-  WEBHOOK_RETRIGGER = 'WEBHOOK_RETRIGGER',
+  WEBHOOK_RETRIGGER = 'WEBHOOK_RETRIGGER_DISABLED',
 }
""",
        TargetMode.PATCH,
    )
    diff = classify_diff(diff, ignore_patterns=[])

    result = run_typescript_analyzer(tmp_path, diff.files)
    packs = build_context_packs([result], diff.files, ReviewConfig(), repo_root=tmp_path) if result else []

    assert result is not None
    symbols_by_name = {symbol.name: symbol for symbol in result.files[0].changed_symbols}
    symbol = symbols_by_name["WEBHOOK_RETRIGGER"]
    assert "Permission" not in symbols_by_name
    assert symbol.kind == "enum-member"
    assert symbol.signature == "Permission.WEBHOOK_RETRIGGER = 'WEBHOOK_RETRIGGER_DISABLED'"
    assert any(
        reference.file == "apps/api/src/admin-webhook.controller.ts"
        and reference.kind == "read"
        and "Permission.WEBHOOK_RETRIGGER" in reference.text
        for reference in symbol.references
    )
    assert result.files[0].related_tests == ["apps/api/src/admin-webhook.controller.test.ts"]
    assert len(packs) == 1
    assert "WEBHOOK_RETRIGGER" in packs[0].id
    assert packs[0].changed_snippets[0].start_line == 3
    assert "WEBHOOK_RETRIGGER_DISABLED" in packs[0].changed_snippets[0].code


def test_typescript_analyzer_collects_removed_enum_member_context(
    tmp_path: Path,
    built_ts_analyzer: None,
) -> None:
    package_src = tmp_path / "packages" / "types" / "src"
    app_src = tmp_path / "apps" / "api" / "src"
    package_src.mkdir(parents=True)
    app_src.mkdir(parents=True)
    (tmp_path / "package.json").write_text('{"workspaces":["apps/*","packages/*"]}', encoding="utf-8")
    (tmp_path / "packages" / "types" / "package.json").write_text(
        '{"name":"@acme/types","types":"./src/index.ts"}',
        encoding="utf-8",
    )
    (tmp_path / "packages" / "types" / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler","strict":true},'
        '"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (tmp_path / "apps" / "api" / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler","strict":true},'
        '"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (package_src / "permission.ts").write_text(
        "export enum Permission {\n"
        "  WEBHOOK_INBOX_VIEW = 'WEBHOOK_INBOX_VIEW',\n"
        "}\n"
        "\n"
        "export const ROLE_PERMISSIONS = {\n"
        "  ADMIN: [Permission.WEBHOOK_INBOX_VIEW],\n"
        "} as const;\n",
        encoding="utf-8",
    )
    (package_src / "index.ts").write_text('export { Permission } from "./permission";\n', encoding="utf-8")
    (app_src / "admin-webhook.controller.ts").write_text(
        'import { Permission } from "@acme/types";\n'
        "\n"
        "export const retriggerPermission = Permission.WEBHOOK_RETRIGGER;\n",
        encoding="utf-8",
    )
    (app_src / "admin-webhook.controller.test.ts").write_text(
        'import { Permission } from "@acme/types";\n'
        "\n"
        "it('still protects webhook retrigger', () => {\n"
        "  expect(Permission.WEBHOOK_RETRIGGER).toBe('WEBHOOK_RETRIGGER');\n"
        "});\n",
        encoding="utf-8",
    )
    diff = parse_unified_diff(
        """diff --git a/packages/types/src/permission.ts b/packages/types/src/permission.ts
index 1111111..2222222 100644
--- a/packages/types/src/permission.ts
+++ b/packages/types/src/permission.ts
@@ -1,4 +1,3 @@
 export enum Permission {
   WEBHOOK_INBOX_VIEW = 'WEBHOOK_INBOX_VIEW',
-  WEBHOOK_RETRIGGER = 'WEBHOOK_RETRIGGER',
 }
""",
        TargetMode.PATCH,
    )
    diff = classify_diff(diff, ignore_patterns=[])

    result = run_typescript_analyzer(tmp_path, diff.files)
    packs = build_context_packs([result], diff.files, ReviewConfig(), repo_root=tmp_path) if result else []

    assert result is not None
    symbols_by_name = {symbol.name: symbol for symbol in result.files[0].changed_symbols}
    symbol = symbols_by_name["WEBHOOK_RETRIGGER"]
    assert "Permission" not in symbols_by_name
    assert symbol.kind == "enum-member"
    assert symbol.signature.startswith("Permission removed entry WEBHOOK_RETRIGGER")
    assert any(
        reference.file == "apps/api/src/admin-webhook.controller.ts"
        and reference.kind == "read"
        and "Permission.WEBHOOK_RETRIGGER" in reference.text
        for reference in symbol.references
    )
    assert result.files[0].related_tests == ["apps/api/src/admin-webhook.controller.test.ts"]
    assert len(packs) == 1
    assert "WEBHOOK_RETRIGGER" in packs[0].id


def test_typescript_analyzer_collects_const_array_object_entry_references(
    tmp_path: Path,
    built_ts_analyzer: None,
) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (tmp_path / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler","strict":true},'
        '"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (src / "bank-routes.ts").write_text(
        "interface BankRouteDefinition {\n"
        "  method: string;\n"
        "  template: string;\n"
        "  signed: boolean;\n"
        "  pattern: RegExp;\n"
        "}\n"
        "\n"
        "const BANK_ROUTES: BankRouteDefinition[] = [\n"
        "  { method: 'POST', template: '/v2/accounts', signed: true, pattern: /^\\/v2\\/accounts$/u },\n"
        "  {\n"
        "    method: 'GET',\n"
        "    template: '/v2/accounts/{accountIdentifier}',\n"
        "    signed: true,\n"
        "    pattern: /^\\/v2\\/accounts\\/[^/]+$/u,\n"
        "  },\n"
        "];\n"
        "\n"
        "export function matchBankRoute(method: string, path: string): { signed: boolean } | undefined {\n"
        "  const normalizedMethod = method.toUpperCase();\n"
        "  const matched = BANK_ROUTES.find(\n"
        "    (definition) => definition.method === normalizedMethod && definition.pattern.test(path),\n"
        "  );\n"
        "  return matched === undefined ? undefined : { signed: matched.signed };\n"
        "}\n",
        encoding="utf-8",
    )
    (src / "bank-routes.test.ts").write_text(
        "import { expect, it } from 'vitest';\n"
        "import { matchBankRoute } from './bank-routes.js';\n"
        "\n"
        "it('keeps GET account lookup unsigned', () => {\n"
        "  expect(matchBankRoute('GET', '/v2/accounts/acc-1')).toEqual({ signed: false });\n"
        "});\n",
        encoding="utf-8",
    )
    diff = parse_unified_diff(
        """diff --git a/src/bank-routes.ts b/src/bank-routes.ts
index 1111111..2222222 100644
--- a/src/bank-routes.ts
+++ b/src/bank-routes.ts
@@ -10,7 +10,7 @@ const BANK_ROUTES: BankRouteDefinition[] = [
   {
     method: 'GET',
     template: '/v2/accounts/{accountIdentifier}',
-    signed: false,
+    signed: true,
     pattern: /^\\/v2\\/accounts\\/[^/]+$/u,
   },
 ];
""",
        TargetMode.PATCH,
    )
    diff = classify_diff(diff, ignore_patterns=[])

    result = run_typescript_analyzer(tmp_path, diff.files)
    packs = build_context_packs([result], diff.files, ReviewConfig(), repo_root=tmp_path) if result else []

    assert result is not None
    symbols_by_name = {symbol.name: symbol for symbol in result.files[0].changed_symbols}
    symbol = symbols_by_name["BANK_ROUTES:GET /v2/accounts/{accountIdentifier}"]
    assert "BANK_ROUTES" not in symbols_by_name
    assert symbol.start_line == 10
    assert symbol.end_line == 15
    assert any(
        reference.file == "src/bank-routes.ts"
        and reference.kind == "read"
        and "const matched = BANK_ROUTES.find(" in reference.text
        for reference in symbol.references
    )
    assert result.files[0].related_tests == ["src/bank-routes.test.ts"]
    assert len(packs) == 1
    assert "BANK_ROUTES:GET /v2/accounts/{accountIdentifier}" in packs[0].id
    assert packs[0].changed_snippets[0].start_line == 10
    assert packs[0].changed_snippets[0].end_line == 15
    assert "const BANK_ROUTES" not in packs[0].changed_snippets[0].code


def test_typescript_analyzer_collects_frozen_const_array_primitive_entry_references(
    tmp_path: Path,
    built_ts_analyzer: None,
) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (tmp_path / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler","strict":true},'
        '"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (src / "webhook-routing.ts").write_text(
        "const IDENTITY_TRAVEL_RULE_EVENTS = Object.freeze([\n"
        "  'applicantKytTxnApproved',\n"
        "  'applicantKytTxnReviewedd',\n"
        "] as const);\n"
        "\n"
        "const IDENTITY_FANOUT_TARGETS: ReadonlyMap<string, readonly string[]> = new Map([\n"
        "  ...IDENTITY_TRAVEL_RULE_EVENTS.map((eventType): [string, readonly string[]] => [\n"
        "    eventType,\n"
        "    ['identity-kyc', 'identity-travel-rule'],\n"
        "  ]),\n"
        "]);\n"
        "\n"
        "export function targetsFor(eventType: string): readonly string[] {\n"
        "  return IDENTITY_FANOUT_TARGETS.get(eventType) ?? ['identity-kyc'];\n"
        "}\n",
        encoding="utf-8",
    )
    (src / "webhook-routing.test.ts").write_text(
        "import { expect, it } from 'vitest';\n"
        "import { targetsFor } from './webhook-routing.js';\n"
        "\n"
        "it('routes reviewed KYT transactions to travel-rule fanout', () => {\n"
        "  expect(targetsFor('applicantKytTxnReviewed')).toEqual(['identity-kyc', 'identity-travel-rule']);\n"
        "});\n",
        encoding="utf-8",
    )
    diff = parse_unified_diff(
        """diff --git a/src/webhook-routing.ts b/src/webhook-routing.ts
index 1111111..2222222 100644
--- a/src/webhook-routing.ts
+++ b/src/webhook-routing.ts
@@ -1,4 +1,4 @@
 const IDENTITY_TRAVEL_RULE_EVENTS = Object.freeze([
   'applicantKytTxnApproved',
-  'applicantKytTxnReviewed',
+  'applicantKytTxnReviewedd',
 ] as const);
""",
        TargetMode.PATCH,
    )
    diff = classify_diff(diff, ignore_patterns=[])

    result = run_typescript_analyzer(tmp_path, diff.files)
    packs = build_context_packs([result], diff.files, ReviewConfig(), repo_root=tmp_path) if result else []

    assert result is not None
    symbols_by_name = {symbol.name: symbol for symbol in result.files[0].changed_symbols}
    symbol = symbols_by_name["IDENTITY_TRAVEL_RULE_EVENTS:applicantKytTxnReviewedd"]
    assert "IDENTITY_TRAVEL_RULE_EVENTS" not in symbols_by_name
    assert symbol.start_line == 3
    assert symbol.end_line == 3
    assert any(
        reference.file == "src/webhook-routing.ts"
        and reference.kind == "read"
        and "IDENTITY_TRAVEL_RULE_EVENTS.map" in reference.text
        for reference in symbol.references
    )
    assert result.files[0].related_tests == ["src/webhook-routing.test.ts"]
    assert len(packs) == 1
    assert "IDENTITY_TRAVEL_RULE_EVENTS:applicantKytTxnReviewedd" in packs[0].id
    assert packs[0].changed_snippets[0].start_line == 3
    assert packs[0].changed_snippets[0].end_line == 3


def test_typescript_analyzer_collects_changed_entry_consumer_impact(
    tmp_path: Path,
    built_ts_analyzer: None,
) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (tmp_path / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler","strict":true},'
        '"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (src / "status.ts").write_text(
        "export function calculateStatus(expectedTargets: readonly string[]): string {\n"
        "  return expectedTargets.length > 1 ? 'DELIVERED' : 'PENDING';\n"
        "}\n",
        encoding="utf-8",
    )
    (src / "routing.ts").write_text(
        "const REVIEWED_EVENTS = Object.freeze([\n"
        "  'applicantKytTxnApproved',\n"
        "  'applicantKytTxnReviewedd',\n"
        "] as const);\n"
        "\n"
        "export class RoutingService {\n"
        "  buildJobs(eventType: string): readonly string[] {\n"
        "    return REVIEWED_EVENTS.includes(eventType as never) ? ['kyc', 'travel-rule'] : ['kyc'];\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    (src / "admin.ts").write_text(
        "import { RoutingService } from './routing.js';\n"
        "import { calculateStatus } from './status.js';\n"
        "\n"
        "export class AdminService {\n"
        "  constructor(private readonly routing = new RoutingService()) {}\n"
        "\n"
        "  status(eventType: string): string {\n"
        "    const expectedTargets = this.routing.buildJobs(eventType);\n"
        "    return calculateStatus(expectedTargets);\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    diff = parse_unified_diff(
        """diff --git a/src/routing.ts b/src/routing.ts
index 1111111..2222222 100644
--- a/src/routing.ts
+++ b/src/routing.ts
@@ -1,4 +1,4 @@
 const REVIEWED_EVENTS = Object.freeze([
   'applicantKytTxnApproved',
-  'applicantKytTxnReviewed',
+  'applicantKytTxnReviewedd',
 ] as const);
""",
        TargetMode.PATCH,
    )
    diff = classify_diff(diff, ignore_patterns=[])

    result = run_typescript_analyzer(tmp_path, diff.files)

    assert result is not None
    symbol = result.files[0].changed_symbols[0]
    assert symbol.name == "REVIEWED_EVENTS:applicantKytTxnReviewedd"
    assert any(
        reference.file == "src/admin.ts"
        and reference.kind == "call"
        and "this.routing.buildJobs(eventType)" in reference.text
        for reference in symbol.references
    )
    assert any(
        callee.file == "src/routing.ts" and callee.kind == "callee" and "buildJobs" in callee.text
        for callee in symbol.callees
    )


def test_typescript_analyzer_collects_map_tuple_entry_references(
    tmp_path: Path,
    built_ts_analyzer: None,
) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (tmp_path / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler","strict":true},'
        '"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (src / "webhook-routing.ts").write_text(
        "const IDENTITY_FANOUT_TARGETS: ReadonlyMap<string, readonly string[]> = new Map([\n"
        "  ['applicantReviewedd', ['identity-kyc', 'identity-pep']],\n"
        "]);\n"
        "\n"
        "export function targetsFor(eventType: string): readonly string[] {\n"
        "  return IDENTITY_FANOUT_TARGETS.get(eventType) ?? ['identity-kyc'];\n"
        "}\n",
        encoding="utf-8",
    )
    (src / "webhook-routing.test.ts").write_text(
        "import { expect, it } from 'vitest';\n"
        "import { targetsFor } from './webhook-routing.js';\n"
        "\n"
        "it('fans out applicantReviewed to KYC and PEP', () => {\n"
        "  expect(targetsFor('applicantReviewed')).toEqual(['identity-kyc', 'identity-pep']);\n"
        "});\n",
        encoding="utf-8",
    )
    diff = parse_unified_diff(
        """diff --git a/src/webhook-routing.ts b/src/webhook-routing.ts
index 1111111..2222222 100644
--- a/src/webhook-routing.ts
+++ b/src/webhook-routing.ts
@@ -1,3 +1,3 @@
 const IDENTITY_FANOUT_TARGETS: ReadonlyMap<string, readonly string[]> = new Map([
-  ['applicantReviewed', ['identity-kyc', 'identity-pep']],
+  ['applicantReviewedd', ['identity-kyc', 'identity-pep']],
 ]);
""",
        TargetMode.PATCH,
    )
    diff = classify_diff(diff, ignore_patterns=[])

    result = run_typescript_analyzer(tmp_path, diff.files)
    packs = build_context_packs([result], diff.files, ReviewConfig(), repo_root=tmp_path) if result else []

    assert result is not None
    symbols_by_name = {symbol.name: symbol for symbol in result.files[0].changed_symbols}
    symbol = symbols_by_name["IDENTITY_FANOUT_TARGETS:applicantReviewedd"]
    assert "IDENTITY_FANOUT_TARGETS" not in symbols_by_name
    assert symbol.start_line == 2
    assert symbol.end_line == 2
    assert any(
        reference.file == "src/webhook-routing.ts"
        and reference.kind == "read"
        and "IDENTITY_FANOUT_TARGETS.get(eventType)" in reference.text
        for reference in symbol.references
    )
    assert result.files[0].related_tests == ["src/webhook-routing.test.ts"]
    assert len(packs) == 1
    assert "IDENTITY_FANOUT_TARGETS:applicantReviewedd" in packs[0].id
    assert packs[0].changed_snippets[0].start_line == 2
    assert packs[0].changed_snippets[0].end_line == 2


def test_typescript_analyzer_collects_factory_call_tuple_entry_references(
    tmp_path: Path,
    built_ts_analyzer: None,
) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (tmp_path / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler","strict":true},'
        '"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (src / "order-status.ts").write_text(
        "export const OrderStatus = {\n"
        "  AWAITING_RECONCILIATION: 'AWAITING_RECONCILIATION',\n"
        "  FAILED: 'FAILED',\n"
        "  CANCELLED: 'CANCELLED',\n"
        "} as const;\n"
        "export type OrderStatus = (typeof OrderStatus)[keyof typeof OrderStatus];\n"
        "\n"
        "export const OrderEvent = {\n"
        "  OPS_RECONCILED_LANDED_CANCEL: 'OPS_RECONCILED_LANDED_CANCEL',\n"
        "} as const;\n"
        "export type OrderEvent = (typeof OrderEvent)[keyof typeof OrderEvent];\n"
        "\n"
        "function createStateMachine<S extends string, E extends string>(\n"
        "  _name: string,\n"
        "  transitions: ReadonlyArray<readonly [S, E, S]>,\n"
        "): (from: S, event: E) => { ok: true; value: S } | { ok: false } {\n"
        "  return (from, event) => {\n"
        "    const match = transitions.find(([candidateFrom, candidateEvent]) => candidateFrom === from && candidateEvent === event);\n"
        "    return match ? { ok: true, value: match[2] } : { ok: false };\n"
        "  };\n"
        "}\n"
        "export const transitionOrder = createStateMachine<OrderStatus, OrderEvent>('order', [\n"
        "  [OrderStatus.AWAITING_RECONCILIATION, OrderEvent.OPS_RECONCILED_LANDED_CANCEL, OrderStatus.FAILED],\n"
        "]);\n",
        encoding="utf-8",
    )
    (src / "reconcile.service.ts").write_text(
        "import { OrderEvent, OrderStatus, transitionOrder } from './order-status.js';\n"
        "\n"
        "export function reconcileOrder(): OrderStatus {\n"
        "  const result = transitionOrder(\n"
        "    OrderStatus.AWAITING_RECONCILIATION,\n"
        "    OrderEvent.OPS_RECONCILED_LANDED_CANCEL,\n"
        "  );\n"
        "  if (!result.ok) throw new Error('invalid transition');\n"
        "  return result.value;\n"
        "}\n",
        encoding="utf-8",
    )
    diff = parse_unified_diff(
        """diff --git a/src/order-status.ts b/src/order-status.ts
index 1111111..2222222 100644
--- a/src/order-status.ts
+++ b/src/order-status.ts
@@ -21,4 +21,4 @@ function createStateMachine<S extends string, E extends string>(
 }
 export const transitionOrder = createStateMachine<OrderStatus, OrderEvent>('order', [
-  [OrderStatus.AWAITING_RECONCILIATION, OrderEvent.OPS_RECONCILED_LANDED_CANCEL, OrderStatus.CANCELLED],
+  [OrderStatus.AWAITING_RECONCILIATION, OrderEvent.OPS_RECONCILED_LANDED_CANCEL, OrderStatus.FAILED],
 ]);
""",
        TargetMode.PATCH,
    )
    diff = classify_diff(diff, ignore_patterns=[])

    result = run_typescript_analyzer(tmp_path, diff.files)
    packs = build_context_packs([result], diff.files, ReviewConfig(), repo_root=tmp_path) if result else []

    assert result is not None
    symbols_by_name = {symbol.name: symbol for symbol in result.files[0].changed_symbols}
    symbol_name = "transitionOrder:OrderStatus.AWAITING_RECONCILIATION OrderEvent.OPS_RECONCILED_LANDED_CANCEL"
    symbol = symbols_by_name[symbol_name]
    assert "transitionOrder" not in symbols_by_name
    assert symbol.start_line == 23
    assert symbol.end_line == 23
    assert any(
        reference.file == "src/reconcile.service.ts"
        and reference.kind == "call"
        and "const result = transitionOrder(" in reference.text
        for reference in symbol.references
    )
    assert len(packs) == 1
    assert symbol_name in packs[0].id
    assert packs[0].changed_snippets[0].start_line == 23
    assert "OrderStatus.FAILED" in packs[0].changed_snippets[0].code


def test_typescript_analyzer_collects_removed_map_tuple_entry_context(
    tmp_path: Path,
    built_ts_analyzer: None,
) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (tmp_path / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler","strict":true},'
        '"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (src / "webhook-routing.ts").write_text(
        "const IDENTITY_FANOUT_TARGETS: ReadonlyMap<string, readonly string[]> = new Map([\n"
        "  ['applicantCreated', ['identity-kyc']],\n"
        "]);\n"
        "\n"
        "export function targetsFor(eventType: string): readonly string[] {\n"
        "  return IDENTITY_FANOUT_TARGETS.get(eventType) ?? ['identity-kyc'];\n"
        "}\n",
        encoding="utf-8",
    )
    (src / "webhook-routing.test.ts").write_text(
        "import { expect, it } from 'vitest';\n"
        "import { targetsFor } from './webhook-routing.js';\n"
        "\n"
        "it('fans out applicantReviewed to KYC and PEP', () => {\n"
        "  expect(targetsFor('applicantReviewed')).toEqual(['identity-kyc', 'identity-pep']);\n"
        "});\n",
        encoding="utf-8",
    )
    diff = parse_unified_diff(
        """diff --git a/src/webhook-routing.ts b/src/webhook-routing.ts
index 1111111..2222222 100644
--- a/src/webhook-routing.ts
+++ b/src/webhook-routing.ts
@@ -1,4 +1,3 @@
 const IDENTITY_FANOUT_TARGETS: ReadonlyMap<string, readonly string[]> = new Map([
-  ['applicantReviewed', ['identity-kyc', 'identity-pep']],
   ['applicantCreated', ['identity-kyc']],
 ]);
""",
        TargetMode.PATCH,
    )
    diff = classify_diff(diff, ignore_patterns=[])

    result = run_typescript_analyzer(tmp_path, diff.files)
    packs = build_context_packs([result], diff.files, ReviewConfig(), repo_root=tmp_path) if result else []

    assert result is not None
    symbols_by_name = {symbol.name: symbol for symbol in result.files[0].changed_symbols}
    symbol = symbols_by_name["IDENTITY_FANOUT_TARGETS:applicantReviewed"]
    assert "IDENTITY_FANOUT_TARGETS:applicantCreated" not in symbols_by_name
    assert symbol.signature.startswith("IDENTITY_FANOUT_TARGETS removed entry")
    assert any(
        reference.file == "src/webhook-routing.ts"
        and reference.kind == "read"
        and "IDENTITY_FANOUT_TARGETS.get(eventType)" in reference.text
        for reference in symbol.references
    )
    assert result.files[0].related_tests == ["src/webhook-routing.test.ts"]
    assert len(packs) == 1
    assert "IDENTITY_FANOUT_TARGETS:applicantReviewed" in packs[0].id
    assert "-  ['applicantReviewed', ['identity-kyc', 'identity-pep']]," in packs[0].diff_snippet


def test_typescript_analyzer_collects_set_primitive_entry_references(
    tmp_path: Path,
    built_ts_analyzer: None,
) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (tmp_path / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler","strict":true},'
        '"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (src / "webhook-routing.ts").write_text(
        "const IDENTITY_BUSINESS_EVENTS = new Set([\n"
        "  'applicantReviewedd',\n"
        "]);\n"
        "\n"
        "export function isBusinessEvent(eventType: string): boolean {\n"
        "  return IDENTITY_BUSINESS_EVENTS.has(eventType) || eventType.startsWith('applicantAction');\n"
        "}\n",
        encoding="utf-8",
    )
    (src / "webhook-routing.test.ts").write_text(
        "import { expect, it } from 'vitest';\n"
        "import { isBusinessEvent } from './webhook-routing.js';\n"
        "\n"
        "it('treats applicantReviewed as business event', () => {\n"
        "  expect(isBusinessEvent('applicantReviewed')).toBe(true);\n"
        "});\n",
        encoding="utf-8",
    )
    diff = parse_unified_diff(
        """diff --git a/src/webhook-routing.ts b/src/webhook-routing.ts
index 1111111..2222222 100644
--- a/src/webhook-routing.ts
+++ b/src/webhook-routing.ts
@@ -1,3 +1,3 @@
 const IDENTITY_BUSINESS_EVENTS = new Set([
-  'applicantReviewed',
+  'applicantReviewedd',
 ]);
""",
        TargetMode.PATCH,
    )
    diff = classify_diff(diff, ignore_patterns=[])

    result = run_typescript_analyzer(tmp_path, diff.files)
    packs = build_context_packs([result], diff.files, ReviewConfig(), repo_root=tmp_path) if result else []

    assert result is not None
    symbols_by_name = {symbol.name: symbol for symbol in result.files[0].changed_symbols}
    symbol = symbols_by_name["IDENTITY_BUSINESS_EVENTS:applicantReviewedd"]
    assert "IDENTITY_BUSINESS_EVENTS" not in symbols_by_name
    assert symbol.start_line == 2
    assert symbol.end_line == 2
    assert any(
        reference.file == "src/webhook-routing.ts"
        and reference.kind == "read"
        and "IDENTITY_BUSINESS_EVENTS.has(eventType)" in reference.text
        for reference in symbol.references
    )
    assert result.files[0].related_tests == ["src/webhook-routing.test.ts"]
    assert len(packs) == 1
    assert "IDENTITY_BUSINESS_EVENTS:applicantReviewedd" in packs[0].id
    assert packs[0].changed_snippets[0].start_line == 2
    assert packs[0].changed_snippets[0].end_line == 2


def test_typescript_analyzer_collects_workspace_package_namespace_import_references(
    tmp_path: Path,
    built_ts_analyzer: None,
) -> None:
    package_src = tmp_path / "packages" / "settings" / "src"
    app_src = tmp_path / "apps" / "api" / "src"
    package_src.mkdir(parents=True)
    app_src.mkdir(parents=True)
    (tmp_path / "package.json").write_text('{"workspaces":["apps/*","packages/*"]}', encoding="utf-8")
    (tmp_path / "packages" / "settings" / "package.json").write_text(
        '{"name":"@acme/settings","types":"./src/index.ts"}',
        encoding="utf-8",
    )
    (tmp_path / "packages" / "settings" / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler","strict":true},'
        '"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (tmp_path / "apps" / "api" / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler","strict":true},'
        '"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (package_src / "index.ts").write_text(
        "export function tenantScopedSettingsKey(tenantId: string, userId: string): string {\n  return userId;\n}\n",
        encoding="utf-8",
    )
    (app_src / "service.ts").write_text(
        'import * as settings from "@acme/settings";\n'
        "export const key = settings.tenantScopedSettingsKey('tenant-a', 'user-a');\n",
        encoding="utf-8",
    )
    diff = parse_unified_diff(
        """diff --git a/packages/settings/src/index.ts b/packages/settings/src/index.ts
index 1111111..2222222 100644
--- a/packages/settings/src/index.ts
+++ b/packages/settings/src/index.ts
@@ -1,3 +1,3 @@
 export function tenantScopedSettingsKey(tenantId: string, userId: string): string {
-  return `${tenantId}:${userId}`;
+  return userId;
 }
""",
        TargetMode.PATCH,
    )
    diff = classify_diff(diff, ignore_patterns=[])

    result = run_typescript_analyzer(tmp_path, diff.files)

    assert result is not None
    references = result.files[0].changed_symbols[0].references
    assert any(
        reference.file == "apps/api/src/service.ts"
        and reference.kind == "call"
        and "settings.tenantScopedSettingsKey('tenant-a', 'user-a')" in reference.text
        for reference in references
    )


def test_typescript_analyzer_treats_jsx_component_usage_as_call_reference(
    tmp_path: Path,
    built_ts_analyzer: None,
) -> None:
    package_src = tmp_path / "packages" / "settings" / "src"
    app_src = tmp_path / "apps" / "web" / "src"
    package_src.mkdir(parents=True)
    app_src.mkdir(parents=True)
    (tmp_path / "package.json").write_text('{"workspaces":["apps/*","packages/*"]}', encoding="utf-8")
    (tmp_path / "packages" / "settings" / "package.json").write_text(
        '{"name":"@acme/settings","types":"./src/index.ts"}',
        encoding="utf-8",
    )
    (tmp_path / "packages" / "settings" / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler","jsx":"react-jsx","strict":true},'
        '"include":["src/**/*.ts","src/**/*.tsx"]}',
        encoding="utf-8",
    )
    (tmp_path / "apps" / "web" / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler","jsx":"react-jsx","strict":true},'
        '"include":["src/**/*.ts","src/**/*.tsx"]}',
        encoding="utf-8",
    )
    (package_src / "index.ts").write_text(
        "export function SettingsPanel(props: { tenantId: string; userId: string }) {\n  return null;\n}\n",
        encoding="utf-8",
    )
    (app_src / "dashboard.tsx").write_text(
        'import { SettingsPanel } from "@acme/settings";\n'
        "\n"
        "export function Dashboard({ userId }: { userId: string }) {\n"
        '  return <SettingsPanel tenantId="tenant-a" userId={userId} />;\n'
        "}\n",
        encoding="utf-8",
    )
    diff = parse_unified_diff(
        """diff --git a/packages/settings/src/index.ts b/packages/settings/src/index.ts
index 1111111..2222222 100644
--- a/packages/settings/src/index.ts
+++ b/packages/settings/src/index.ts
@@ -1,3 +1,3 @@
 export function SettingsPanel(props: { tenantId: string; userId: string }) {
-  return props.tenantId;
+  return props.userId;
 }
""",
        TargetMode.PATCH,
    )
    diff = classify_diff(diff, ignore_patterns=[])

    result = run_typescript_analyzer(tmp_path, diff.files)

    assert result is not None
    references = result.files[0].changed_symbols[0].references
    assert any(
        reference.file == "apps/web/src/dashboard.tsx"
        and reference.kind == "call"
        and "<SettingsPanel tenantId=" in reference.text
        for reference in references
    )


def test_typescript_analyzer_treats_jsx_namespace_component_usage_as_call_reference(
    tmp_path: Path,
    built_ts_analyzer: None,
) -> None:
    package_src = tmp_path / "packages" / "settings" / "src"
    app_src = tmp_path / "apps" / "web" / "src"
    package_src.mkdir(parents=True)
    app_src.mkdir(parents=True)
    (tmp_path / "package.json").write_text('{"workspaces":["apps/*","packages/*"]}', encoding="utf-8")
    (tmp_path / "packages" / "settings" / "package.json").write_text(
        '{"name":"@acme/settings","types":"./src/index.ts"}',
        encoding="utf-8",
    )
    (tmp_path / "packages" / "settings" / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler","jsx":"react-jsx","strict":true},'
        '"include":["src/**/*.ts","src/**/*.tsx"]}',
        encoding="utf-8",
    )
    (tmp_path / "apps" / "web" / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler","jsx":"react-jsx","strict":true},'
        '"include":["src/**/*.ts","src/**/*.tsx"]}',
        encoding="utf-8",
    )
    (package_src / "index.ts").write_text(
        "export function SettingsPanel(props: { tenantId: string; userId: string }) {\n  return null;\n}\n",
        encoding="utf-8",
    )
    (app_src / "dashboard.tsx").write_text(
        'import * as settings from "@acme/settings";\n'
        "\n"
        "export function Dashboard({ userId }: { userId: string }) {\n"
        '  return <settings.SettingsPanel tenantId="tenant-a" userId={userId} />;\n'
        "}\n",
        encoding="utf-8",
    )
    diff = parse_unified_diff(
        """diff --git a/packages/settings/src/index.ts b/packages/settings/src/index.ts
index 1111111..2222222 100644
--- a/packages/settings/src/index.ts
+++ b/packages/settings/src/index.ts
@@ -1,3 +1,3 @@
 export function SettingsPanel(props: { tenantId: string; userId: string }) {
-  return props.tenantId;
+  return props.userId;
 }
""",
        TargetMode.PATCH,
    )
    diff = classify_diff(diff, ignore_patterns=[])

    result = run_typescript_analyzer(tmp_path, diff.files)

    assert result is not None
    references = result.files[0].changed_symbols[0].references
    assert any(
        reference.file == "apps/web/src/dashboard.tsx"
        and reference.kind == "call"
        and "<settings.SettingsPanel tenantId=" in reference.text
        for reference in references
    )


def test_typescript_analyzer_collects_anonymous_default_function_references(
    tmp_path: Path,
    built_ts_analyzer: None,
) -> None:
    package_src = tmp_path / "packages" / "settings" / "src"
    app_src = tmp_path / "apps" / "web" / "src"
    package_src.mkdir(parents=True)
    app_src.mkdir(parents=True)
    (tmp_path / "package.json").write_text('{"workspaces":["apps/*","packages/*"]}', encoding="utf-8")
    (tmp_path / "packages" / "settings" / "package.json").write_text(
        '{"name":"@acme/settings","types":"./src/index.tsx"}',
        encoding="utf-8",
    )
    (tmp_path / "packages" / "settings" / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler","jsx":"react-jsx","strict":true},'
        '"include":["src/**/*.ts","src/**/*.tsx"]}',
        encoding="utf-8",
    )
    (tmp_path / "apps" / "web" / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler","jsx":"react-jsx","strict":true},'
        '"include":["src/**/*.ts","src/**/*.tsx"]}',
        encoding="utf-8",
    )
    (package_src / "index.tsx").write_text(
        "export default function(props: { tenantId: string; userId: string }) {\n  return props.userId;\n}\n",
        encoding="utf-8",
    )
    (app_src / "dashboard.tsx").write_text(
        'import SettingsPanel from "@acme/settings";\n'
        "\n"
        "export function Dashboard({ userId }: { userId: string }) {\n"
        '  return <SettingsPanel tenantId="tenant-a" userId={userId} />;\n'
        "}\n",
        encoding="utf-8",
    )
    diff = parse_unified_diff(
        """diff --git a/packages/settings/src/index.tsx b/packages/settings/src/index.tsx
index 1111111..2222222 100644
--- a/packages/settings/src/index.tsx
+++ b/packages/settings/src/index.tsx
@@ -1,3 +1,3 @@
 export default function(props: { tenantId: string; userId: string }) {
-  return props.tenantId;
+  return props.userId;
 }
""",
        TargetMode.PATCH,
    )
    diff = classify_diff(diff, ignore_patterns=[])

    result = run_typescript_analyzer(tmp_path, diff.files)

    assert result is not None
    symbols_by_name = {symbol.name: symbol for symbol in result.files[0].changed_symbols}
    assert symbols_by_name["default"].exported is True
    assert any(
        reference.file == "apps/web/src/dashboard.tsx"
        and reference.kind == "call"
        and "<SettingsPanel tenantId=" in reference.text
        for reference in symbols_by_name["default"].references
    )


def test_typescript_analyzer_collects_workspace_package_require_references(
    tmp_path: Path,
    built_ts_analyzer: None,
) -> None:
    package_src = tmp_path / "packages" / "settings" / "src"
    app_src = tmp_path / "apps" / "api" / "src"
    package_src.mkdir(parents=True)
    app_src.mkdir(parents=True)
    (tmp_path / "package.json").write_text('{"workspaces":["apps/*","packages/*"]}', encoding="utf-8")
    (tmp_path / "packages" / "settings" / "package.json").write_text(
        '{"name":"@acme/settings","types":"./src/index.ts"}',
        encoding="utf-8",
    )
    (tmp_path / "packages" / "settings" / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler","strict":true},'
        '"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (tmp_path / "apps" / "api" / "jsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"CommonJS","moduleResolution":"Node","allowJs":true},'
        '"include":["src/**/*.js"]}',
        encoding="utf-8",
    )
    (package_src / "index.ts").write_text(
        "export function tenantScopedSettingsKey(tenantId: string, userId: string): string {\n  return userId;\n}\n",
        encoding="utf-8",
    )
    (app_src / "service.js").write_text(
        'const { tenantScopedSettingsKey } = require("@acme/settings");\n'
        "exports.key = tenantScopedSettingsKey('tenant-a', 'user-a');\n",
        encoding="utf-8",
    )
    diff = parse_unified_diff(
        """diff --git a/packages/settings/src/index.ts b/packages/settings/src/index.ts
index 1111111..2222222 100644
--- a/packages/settings/src/index.ts
+++ b/packages/settings/src/index.ts
@@ -1,3 +1,3 @@
 export function tenantScopedSettingsKey(tenantId: string, userId: string): string {
-  return `${tenantId}:${userId}`;
+  return userId;
 }
""",
        TargetMode.PATCH,
    )
    diff = classify_diff(diff, ignore_patterns=[])

    result = run_typescript_analyzer(tmp_path, diff.files)

    assert result is not None
    references = result.files[0].changed_symbols[0].references
    assert any(
        reference.file == "apps/api/src/service.js"
        and reference.kind == "call"
        and "tenantScopedSettingsKey('tenant-a', 'user-a')" in reference.text
        for reference in references
    )


def test_typescript_analyzer_treats_commonjs_object_exports_as_package_exports(
    tmp_path: Path,
    built_ts_analyzer: None,
) -> None:
    package_src = tmp_path / "packages" / "settings" / "src"
    app_src = tmp_path / "apps" / "api" / "src"
    package_src.mkdir(parents=True)
    app_src.mkdir(parents=True)
    (tmp_path / "package.json").write_text('{"workspaces":["apps/*","packages/*"]}', encoding="utf-8")
    (tmp_path / "packages" / "settings" / "package.json").write_text(
        '{"name":"@acme/settings","main":"./src/index.js"}',
        encoding="utf-8",
    )
    (tmp_path / "packages" / "settings" / "jsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"CommonJS","moduleResolution":"Node","allowJs":true},'
        '"include":["src/**/*.js"]}',
        encoding="utf-8",
    )
    (tmp_path / "apps" / "api" / "jsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"CommonJS","moduleResolution":"Node","allowJs":true},'
        '"include":["src/**/*.js"]}',
        encoding="utf-8",
    )
    (package_src / "index.js").write_text(
        "function tenantScopedSettingsKey(tenantId, userId) {\n"
        "  return userId;\n"
        "}\n"
        "\n"
        "module.exports = { tenantScopedSettingsKey };\n",
        encoding="utf-8",
    )
    (app_src / "service.js").write_text(
        'const { tenantScopedSettingsKey } = require("@acme/settings");\n'
        "exports.key = tenantScopedSettingsKey('tenant-a', 'user-a');\n",
        encoding="utf-8",
    )
    diff = parse_unified_diff(
        """diff --git a/packages/settings/src/index.js b/packages/settings/src/index.js
index 1111111..2222222 100644
--- a/packages/settings/src/index.js
+++ b/packages/settings/src/index.js
@@ -1,3 +1,3 @@
 function tenantScopedSettingsKey(tenantId, userId) {
-  return `${tenantId}:${userId}`;
+  return userId;
 }
""",
        TargetMode.PATCH,
    )
    diff = classify_diff(diff, ignore_patterns=[])

    result = run_typescript_analyzer(tmp_path, diff.files)

    assert result is not None
    symbol = result.files[0].changed_symbols[0]
    assert symbol.exported is True
    assert any(
        reference.file == "apps/api/src/service.js"
        and reference.kind == "call"
        and "tenantScopedSettingsKey('tenant-a', 'user-a')" in reference.text
        for reference in symbol.references
    )


def test_typescript_analyzer_treats_commonjs_default_export_require_as_package_export(
    tmp_path: Path,
    built_ts_analyzer: None,
) -> None:
    package_src = tmp_path / "packages" / "settings" / "src"
    app_src = tmp_path / "apps" / "api" / "src"
    package_src.mkdir(parents=True)
    app_src.mkdir(parents=True)
    (tmp_path / "package.json").write_text('{"workspaces":["apps/*","packages/*"]}', encoding="utf-8")
    (tmp_path / "packages" / "settings" / "package.json").write_text(
        '{"name":"@acme/settings","main":"./src/index.js"}',
        encoding="utf-8",
    )
    (tmp_path / "packages" / "settings" / "jsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"CommonJS","moduleResolution":"Node","allowJs":true},'
        '"include":["src/**/*.js"]}',
        encoding="utf-8",
    )
    (tmp_path / "apps" / "api" / "jsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"CommonJS","moduleResolution":"Node","allowJs":true},'
        '"include":["src/**/*.js"]}',
        encoding="utf-8",
    )
    (package_src / "index.js").write_text(
        "function tenantScopedSettingsKey(tenantId, userId) {\n"
        "  return userId;\n"
        "}\n"
        "\n"
        "module.exports = tenantScopedSettingsKey;\n",
        encoding="utf-8",
    )
    (app_src / "service.js").write_text(
        'const tenantScopedSettingsKey = require("@acme/settings");\n'
        "exports.key = tenantScopedSettingsKey('tenant-a', 'user-a');\n",
        encoding="utf-8",
    )
    diff = parse_unified_diff(
        """diff --git a/packages/settings/src/index.js b/packages/settings/src/index.js
index 1111111..2222222 100644
--- a/packages/settings/src/index.js
+++ b/packages/settings/src/index.js
@@ -1,3 +1,3 @@
 function tenantScopedSettingsKey(tenantId, userId) {
-  return `${tenantId}:${userId}`;
+  return userId;
 }
""",
        TargetMode.PATCH,
    )
    diff = classify_diff(diff, ignore_patterns=[])

    result = run_typescript_analyzer(tmp_path, diff.files)

    assert result is not None
    symbol = result.files[0].changed_symbols[0]
    assert symbol.exported is True
    assert any(
        reference.file == "apps/api/src/service.js"
        and reference.kind == "call"
        and "tenantScopedSettingsKey('tenant-a', 'user-a')" in reference.text
        for reference in symbol.references
    )


def test_typescript_analyzer_collects_import_equals_require_references(
    tmp_path: Path,
    built_ts_analyzer: None,
) -> None:
    package_src = tmp_path / "packages" / "settings" / "src"
    app_src = tmp_path / "apps" / "api" / "src"
    package_src.mkdir(parents=True)
    app_src.mkdir(parents=True)
    (tmp_path / "package.json").write_text('{"workspaces":["apps/*","packages/*"]}', encoding="utf-8")
    (tmp_path / "packages" / "settings" / "package.json").write_text(
        '{"name":"@acme/settings","types":"./src/index.ts"}',
        encoding="utf-8",
    )
    (tmp_path / "packages" / "settings" / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"CommonJS","moduleResolution":"Node","strict":true},'
        '"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (tmp_path / "apps" / "api" / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"CommonJS","moduleResolution":"Node","strict":true},'
        '"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (package_src / "index.ts").write_text(
        "function tenantScopedSettingsKey(tenantId: string, userId: string): string {\n"
        "  return userId;\n"
        "}\n"
        "\n"
        "export = tenantScopedSettingsKey;\n",
        encoding="utf-8",
    )
    (app_src / "service.ts").write_text(
        'import tenantScopedSettingsKey = require("@acme/settings");\n'
        "export const key = tenantScopedSettingsKey('tenant-a', 'user-a');\n",
        encoding="utf-8",
    )
    diff = parse_unified_diff(
        """diff --git a/packages/settings/src/index.ts b/packages/settings/src/index.ts
index 1111111..2222222 100644
--- a/packages/settings/src/index.ts
+++ b/packages/settings/src/index.ts
@@ -1,3 +1,3 @@
 function tenantScopedSettingsKey(tenantId: string, userId: string): string {
-  return `${tenantId}:${userId}`;
+  return userId;
 }
""",
        TargetMode.PATCH,
    )
    diff = classify_diff(diff, ignore_patterns=[])

    result = run_typescript_analyzer(tmp_path, diff.files)

    assert result is not None
    references = result.files[0].changed_symbols[0].references
    assert any(
        reference.file == "apps/api/src/service.ts"
        and reference.kind == "call"
        and "tenantScopedSettingsKey('tenant-a', 'user-a')" in reference.text
        for reference in references
    )


def test_typescript_analyzer_collects_import_equals_namespace_require_references(
    tmp_path: Path,
    built_ts_analyzer: None,
) -> None:
    package_src = tmp_path / "packages" / "settings" / "src"
    app_src = tmp_path / "apps" / "api" / "src"
    package_src.mkdir(parents=True)
    app_src.mkdir(parents=True)
    (tmp_path / "package.json").write_text('{"workspaces":["apps/*","packages/*"]}', encoding="utf-8")
    (tmp_path / "packages" / "settings" / "package.json").write_text(
        '{"name":"@acme/settings","types":"./src/index.ts"}',
        encoding="utf-8",
    )
    (tmp_path / "packages" / "settings" / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"CommonJS","moduleResolution":"Node","strict":true},'
        '"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (tmp_path / "apps" / "api" / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"CommonJS","moduleResolution":"Node","strict":true},'
        '"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (package_src / "index.ts").write_text(
        "export function tenantScopedSettingsKey(tenantId: string, userId: string): string {\n  return userId;\n}\n",
        encoding="utf-8",
    )
    (app_src / "service.ts").write_text(
        'import settings = require("@acme/settings");\n'
        "export const key = settings.tenantScopedSettingsKey('tenant-a', 'user-a');\n",
        encoding="utf-8",
    )
    diff = parse_unified_diff(
        """diff --git a/packages/settings/src/index.ts b/packages/settings/src/index.ts
index 1111111..2222222 100644
--- a/packages/settings/src/index.ts
+++ b/packages/settings/src/index.ts
@@ -1,3 +1,3 @@
 export function tenantScopedSettingsKey(tenantId: string, userId: string): string {
-  return `${tenantId}:${userId}`;
+  return userId;
 }
""",
        TargetMode.PATCH,
    )
    diff = classify_diff(diff, ignore_patterns=[])

    result = run_typescript_analyzer(tmp_path, diff.files)

    assert result is not None
    references = result.files[0].changed_symbols[0].references
    assert any(
        reference.file == "apps/api/src/service.ts"
        and reference.kind == "call"
        and "settings.tenantScopedSettingsKey('tenant-a', 'user-a')" in reference.text
        for reference in references
    )


def test_typescript_analyzer_collects_workspace_package_dynamic_import_references(
    tmp_path: Path,
    built_ts_analyzer: None,
) -> None:
    package_src = tmp_path / "packages" / "settings" / "src"
    app_src = tmp_path / "apps" / "api" / "src"
    package_src.mkdir(parents=True)
    app_src.mkdir(parents=True)
    (tmp_path / "package.json").write_text('{"workspaces":["apps/*","packages/*"]}', encoding="utf-8")
    (tmp_path / "packages" / "settings" / "package.json").write_text(
        '{"name":"@acme/settings","types":"./src/index.ts"}',
        encoding="utf-8",
    )
    (tmp_path / "packages" / "settings" / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler","strict":true},'
        '"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (tmp_path / "apps" / "api" / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler","strict":true},'
        '"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (package_src / "index.ts").write_text(
        "export function tenantScopedSettingsKey(tenantId: string, userId: string): string {\n  return userId;\n}\n",
        encoding="utf-8",
    )
    (app_src / "dynamic-service.ts").write_text(
        "export async function buildDynamicSettingsCachePreview(userId: string) {\n"
        '  const { tenantScopedSettingsKey } = await import("@acme/settings");\n'
        "  return tenantScopedSettingsKey('tenant-a', userId);\n"
        "}\n",
        encoding="utf-8",
    )
    (app_src / "dynamic-namespace-service.ts").write_text(
        "export async function buildDynamicNamespaceSettingsCachePreview(userId: string) {\n"
        '  const settings = await import("@acme/settings");\n'
        "  return settings.tenantScopedSettingsKey('tenant-b', userId);\n"
        "}\n",
        encoding="utf-8",
    )
    diff = parse_unified_diff(
        """diff --git a/packages/settings/src/index.ts b/packages/settings/src/index.ts
index 1111111..2222222 100644
--- a/packages/settings/src/index.ts
+++ b/packages/settings/src/index.ts
@@ -1,3 +1,3 @@
 export function tenantScopedSettingsKey(tenantId: string, userId: string): string {
-  return `${tenantId}:${userId}`;
+  return userId;
 }
""",
        TargetMode.PATCH,
    )
    diff = classify_diff(diff, ignore_patterns=[])

    result = run_typescript_analyzer(tmp_path, diff.files)

    assert result is not None
    references = result.files[0].changed_symbols[0].references
    assert any(
        reference.file == "apps/api/src/dynamic-service.ts"
        and reference.kind == "call"
        and "tenantScopedSettingsKey('tenant-a', userId)" in reference.text
        for reference in references
    )
    assert any(
        reference.file == "apps/api/src/dynamic-namespace-service.ts"
        and reference.kind == "call"
        and "settings.tenantScopedSettingsKey('tenant-b', userId)" in reference.text
        for reference in references
    )


def test_typescript_analyzer_collects_dynamic_import_path_alias_references(
    tmp_path: Path,
    built_ts_analyzer: None,
) -> None:
    plugin_src = tmp_path / "apps" / "simulator" / "src" / "simulators" / "bank"
    cli_src = tmp_path / "apps" / "simulator" / "src" / "cli"
    plugin_src.mkdir(parents=True)
    cli_src.mkdir(parents=True)
    (tmp_path / "package.json").write_text('{"workspaces":["apps/*"]}', encoding="utf-8")
    (tmp_path / "apps" / "simulator" / "package.json").write_text(
        '{"name":"@acme/simulator","type":"module"}',
        encoding="utf-8",
    )
    (tmp_path / "apps" / "simulator" / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler","strict":true,'
        '"baseUrl":".","paths":{"@sim/*":["./src/*"]}},"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (plugin_src / "plugin.ts").write_text(
        "export interface SimulatorPlugin {\n"
        "  name: string;\n"
        "  enabled: boolean;\n"
        "}\n"
        "\n"
        "export const bankPlugin: SimulatorPlugin = {\n"
        '  name: "bank",\n'
        "  enabled: false,\n"
        "};\n",
        encoding="utf-8",
    )
    (cli_src / "simulator-loader.ts").write_text(
        "export async function loadPlugin(name: string) {\n"
        '  if (name === "bank") {\n'
        '    const mod = await import("@sim/simulators/bank/plugin");\n'
        "    return mod.bankPlugin;\n"
        "  }\n"
        "  return null;\n"
        "}\n",
        encoding="utf-8",
    )
    diff = parse_unified_diff(
        """diff --git a/apps/simulator/src/simulators/bank/plugin.ts b/apps/simulator/src/simulators/bank/plugin.ts
index 1111111..2222222 100644
--- a/apps/simulator/src/simulators/bank/plugin.ts
+++ b/apps/simulator/src/simulators/bank/plugin.ts
@@ -5,6 +5,6 @@ export interface SimulatorPlugin {
 export const bankPlugin: SimulatorPlugin = {
   name: "bank",
-  enabled: true,
+  enabled: false,
 };
""",
        TargetMode.PATCH,
    )
    diff = classify_diff(diff, ignore_patterns=[])

    result = run_typescript_analyzer(tmp_path, diff.files)

    assert result is not None
    references = result.files[0].changed_symbols[0].references
    assert any(
        reference.file == "apps/simulator/src/cli/simulator-loader.ts"
        and reference.kind == "read"
        and "return mod.bankPlugin" in reference.text
        for reference in references
    )
    assert any(
        reference.file == "apps/simulator/src/cli/simulator-loader.ts"
        and reference.kind == "import"
        and 'import("@sim/simulators/bank/plugin")' in reference.text
        for reference in references
    )


def test_typescript_analyzer_collects_exported_class_method_references(
    tmp_path: Path,
    built_ts_analyzer: None,
) -> None:
    package_src = tmp_path / "packages" / "settings" / "src"
    app_src = tmp_path / "apps" / "api" / "src"
    package_src.mkdir(parents=True)
    app_src.mkdir(parents=True)
    (tmp_path / "package.json").write_text('{"workspaces":["apps/*","packages/*"]}', encoding="utf-8")
    (tmp_path / "packages" / "settings" / "package.json").write_text(
        '{"name":"@acme/settings","types":"./src/index.ts"}',
        encoding="utf-8",
    )
    (tmp_path / "packages" / "settings" / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler","strict":true},'
        '"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (tmp_path / "apps" / "api" / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler","strict":true},'
        '"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (package_src / "index.ts").write_text(
        "export class SettingsClient {\n"
        "  buildTenantSettingsKey(tenantId: string, userId: string): string {\n"
        "    return userId;\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    (app_src / "service.ts").write_text(
        'import { SettingsClient } from "@acme/settings";\n'
        "const client = new SettingsClient();\n"
        "export const key = client.buildTenantSettingsKey('tenant-a', 'user-a');\n",
        encoding="utf-8",
    )
    diff = parse_unified_diff(
        """diff --git a/packages/settings/src/index.ts b/packages/settings/src/index.ts
index 1111111..2222222 100644
--- a/packages/settings/src/index.ts
+++ b/packages/settings/src/index.ts
@@ -1,5 +1,5 @@
 export class SettingsClient {
   buildTenantSettingsKey(tenantId: string, userId: string): string {
-    return `${tenantId}:${userId}`;
+    return userId;
   }
 }
""",
        TargetMode.PATCH,
    )
    diff = classify_diff(diff, ignore_patterns=[])

    result = run_typescript_analyzer(tmp_path, diff.files)

    assert result is not None
    symbols_by_name = {symbol.name: symbol for symbol in result.files[0].changed_symbols}
    references = symbols_by_name["buildTenantSettingsKey"].references
    assert any(
        reference.file == "apps/api/src/service.ts"
        and reference.kind == "call"
        and "client.buildTenantSettingsKey('tenant-a', 'user-a')" in reference.text
        for reference in references
    )


def test_typescript_analyzer_keeps_this_super_and_subclass_method_references(
    tmp_path: Path,
    built_ts_analyzer: None,
) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (tmp_path / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler","strict":true},'
        '"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (src / "service.ts").write_text(
        "export class BaseService {\n"
        "  changed(id: string): string {\n"
        "    return id;\n"
        "  }\n"
        "\n"
        "  callBase(id: string): string {\n"
        "    return this.changed(id);\n"
        "  }\n"
        "}\n"
        "\n"
        "export class DerivedService extends BaseService {\n"
        "  callDerived(id: string): string {\n"
        "    return this.changed(id);\n"
        "  }\n"
        "}\n"
        "\n"
        "export class OverrideService extends BaseService {\n"
        "  changed(id: string): string {\n"
        "    return super.changed(id);\n"
        "  }\n"
        "}\n"
        "\n"
        "const derived = new DerivedService();\n"
        "export const value = derived.changed('id');\n",
        encoding="utf-8",
    )
    diff = parse_unified_diff(
        """diff --git a/src/service.ts b/src/service.ts
index 1111111..2222222 100644
--- a/src/service.ts
+++ b/src/service.ts
@@ -1,4 +1,4 @@
export class BaseService {
  changed(id: string): string {
-    return `${id}:old`;
+    return id;
  }
""",
        TargetMode.PATCH,
    )
    diff = classify_diff(diff, ignore_patterns=[])

    result = run_typescript_analyzer(tmp_path, diff.files)

    assert result is not None
    references = {symbol.name: symbol for symbol in result.files[0].changed_symbols}["changed"].references
    assert any(
        reference.kind == "call" and reference.file == "src/service.ts" and "this.changed(id)" in reference.text
        for reference in references
    )
    assert any(
        reference.kind == "call" and reference.file == "src/service.ts" and "super.changed(id)" in reference.text
        for reference in references
    )
    assert any(
        reference.kind == "call" and reference.file == "src/service.ts" and "derived.changed('id')" in reference.text
        for reference in references
    )


def test_typescript_analyzer_collects_nest_provider_token_injection_references(
    tmp_path: Path,
    built_ts_analyzer: None,
) -> None:
    package_src = tmp_path / "packages" / "settlement" / "src"
    app_src = tmp_path / "apps" / "api" / "src"
    package_src.mkdir(parents=True)
    app_src.mkdir(parents=True)
    (tmp_path / "package.json").write_text('{"workspaces":["apps/*","packages/*"]}', encoding="utf-8")
    (tmp_path / "packages" / "settlement" / "package.json").write_text(
        '{"name":"@acme/settlement","types":"./src/index.ts"}',
        encoding="utf-8",
    )
    (tmp_path / "packages" / "settlement" / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler","strict":true},'
        '"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (tmp_path / "apps" / "api" / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler","strict":true,'
        '"experimentalDecorators":true},"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (package_src / "index.ts").write_text(
        "export class InternalBankTransferExecutor {\n"
        "  execute(stepType: string): string {\n"
        "    return stepType;\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    (app_src / "settlement.module.ts").write_text(
        'import { Module } from "@nestjs/common";\n'
        'import { InternalBankTransferExecutor } from "@acme/settlement";\n'
        'import { STEP_EXECUTOR_PORT } from "./step-executor.port";\n'
        "\n"
        "@Module({\n"
        "  providers: [\n"
        "    InternalBankTransferExecutor,\n"
        "    {\n"
        "      provide: STEP_EXECUTOR_PORT,\n"
        "      useFactory: (bank: InternalBankTransferExecutor) => [bank],\n"
        "      inject: [InternalBankTransferExecutor],\n"
        "    },\n"
        "  ],\n"
        "})\n"
        "export class SettlementModule {}\n",
        encoding="utf-8",
    )
    (app_src / "step-executor.port.ts").write_text(
        'export const STEP_EXECUTOR_PORT = Symbol("STEP_EXECUTOR_PORT");\n'
        "export interface StepExecutorPort {\n"
        "  execute(stepType: string): string;\n"
        "}\n",
        encoding="utf-8",
    )
    (app_src / "step-executor.registry.ts").write_text(
        'import { Inject, Injectable } from "@nestjs/common";\n'
        'import { STEP_EXECUTOR_PORT, type StepExecutorPort } from "./step-executor.port";\n'
        "\n"
        "@Injectable()\n"
        "export class StepExecutorRegistry {\n"
        "  constructor(@Inject(STEP_EXECUTOR_PORT) private readonly executors: ReadonlyArray<StepExecutorPort>) {}\n"
        "\n"
        "  run(stepType: string): string | undefined {\n"
        "    return this.executors[0]?.execute(stepType);\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    diff = parse_unified_diff(
        """diff --git a/packages/settlement/src/index.ts b/packages/settlement/src/index.ts
index 1111111..2222222 100644
--- a/packages/settlement/src/index.ts
+++ b/packages/settlement/src/index.ts
@@ -1,5 +1,5 @@
 export class InternalBankTransferExecutor {
   execute(stepType: string): string {
-    return `bank:${stepType}`;
+    return stepType;
   }
 }
""",
        TargetMode.PATCH,
    )
    diff = classify_diff(diff, ignore_patterns=[])

    result = run_typescript_analyzer(tmp_path, diff.files)

    assert result is not None
    symbols_by_name = {symbol.name: symbol for symbol in result.files[0].changed_symbols}
    references = symbols_by_name["execute"].references
    assert any(
        reference.file == "apps/api/src/settlement.module.ts"
        and "inject: [InternalBankTransferExecutor]" in reference.text
        for reference in references
    )
    assert any(
        reference.file == "apps/api/src/step-executor.registry.ts" and "@Inject(STEP_EXECUTOR_PORT)" in reference.text
        for reference in references
    )


def test_typescript_analyzer_collects_injected_service_method_call_references(
    tmp_path: Path,
    built_ts_analyzer: None,
) -> None:
    service_src = tmp_path / "packages" / "webhooks" / "src"
    app_src = tmp_path / "apps" / "api" / "src"
    service_src.mkdir(parents=True)
    app_src.mkdir(parents=True)
    (tmp_path / "package.json").write_text('{"workspaces":["apps/*","packages/*"]}', encoding="utf-8")
    (tmp_path / "packages" / "webhooks" / "package.json").write_text(
        '{"name":"@acme/webhooks","types":"./src/index.ts"}',
        encoding="utf-8",
    )
    (tmp_path / "packages" / "webhooks" / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler","strict":true,'
        '"experimentalDecorators":true},"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (tmp_path / "apps" / "api" / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler","strict":true,'
        '"experimentalDecorators":true},"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (service_src / "index.ts").write_text(
        "export class AdminWebhookOpsService {\n"
        "  retrigger(id: string, actorId: string): string {\n"
        "    return id;\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    (app_src / "tokens.ts").write_text(
        "export const ADMIN_WEBHOOK_OPS_SERVICE = Symbol('ADMIN_WEBHOOK_OPS_SERVICE');\n",
        encoding="utf-8",
    )
    (app_src / "webhook.module.ts").write_text(
        'import { AdminWebhookOpsService } from "@acme/webhooks";\n'
        'import { ADMIN_WEBHOOK_OPS_SERVICE } from "./tokens";\n'
        "\n"
        "export const providers = [\n"
        "  {\n"
        "    provide: ADMIN_WEBHOOK_OPS_SERVICE,\n"
        "    useFactory: (): AdminWebhookOpsService => new AdminWebhookOpsService(),\n"
        "  },\n"
        "];\n",
        encoding="utf-8",
    )
    (app_src / "admin-webhook.controller.ts").write_text(
        'import { Inject } from "@nestjs/common";\n'
        'import type { AdminWebhookOpsService } from "@acme/webhooks";\n'
        'import { ADMIN_WEBHOOK_OPS_SERVICE } from "./tokens";\n'
        "\n"
        "type OpsService = AdminWebhookOpsService;\n"
        "\n"
        "export class AdminWebhookController {\n"
        "  constructor(@Inject(ADMIN_WEBHOOK_OPS_SERVICE) private readonly ops: OpsService) {}\n"
        "\n"
        "  retrigger(id: string, actorId: string): string {\n"
        "    return this.ops.retrigger(id, actorId);\n"
        "  }\n"
        "}\n"
        "\n"
        "export function unrelated(ops: { retrigger(id: string, actorId: string): string }): string {\n"
        "  return ops.retrigger('local', 'actor');\n"
        "}\n"
        "\n"
        "const unrelatedOps = {\n"
        "  retrigger(id: string, actorId: string): string {\n"
        "    return `${actorId}:${id}`;\n"
        "  },\n"
        "};\n"
        "export const unrelated = unrelatedOps.retrigger('local', 'actor');\n",
        encoding="utf-8",
    )
    diff = parse_unified_diff(
        """diff --git a/packages/webhooks/src/index.ts b/packages/webhooks/src/index.ts
index 1111111..2222222 100644
--- a/packages/webhooks/src/index.ts
+++ b/packages/webhooks/src/index.ts
@@ -1,5 +1,5 @@
 export class AdminWebhookOpsService {
   retrigger(id: string, actorId: string): string {
-    return `${actorId}:${id}`;
+    return id;
   }
 }
""",
        TargetMode.PATCH,
    )
    diff = classify_diff(diff, ignore_patterns=[])

    result = run_typescript_analyzer(tmp_path, diff.files)

    assert result is not None
    symbols_by_name = {symbol.name: symbol for symbol in result.files[0].changed_symbols}
    references = symbols_by_name["retrigger"].references
    assert any(
        reference.file == "apps/api/src/admin-webhook.controller.ts"
        and reference.kind == "call"
        and "this.ops.retrigger(id, actorId)" in reference.text
        for reference in references
    )
    assert any(
        reference.file == "apps/api/src/admin-webhook.controller.ts"
        and reference.kind == "import"
        and "AdminWebhookOpsService" in reference.text
        for reference in references
    )
    assert all("ops.retrigger('local', 'actor')" not in reference.text for reference in references)
    assert all("unrelatedOps.retrigger" not in reference.text for reference in references)


def test_typescript_analyzer_treats_export_statements_as_package_exports(
    tmp_path: Path,
    built_ts_analyzer: None,
) -> None:
    package_src = tmp_path / "packages" / "settings" / "src"
    app_src = tmp_path / "apps" / "api" / "src"
    package_src.mkdir(parents=True)
    app_src.mkdir(parents=True)
    (tmp_path / "package.json").write_text('{"workspaces":["apps/*","packages/*"]}', encoding="utf-8")
    (tmp_path / "packages" / "settings" / "package.json").write_text(
        '{"name":"@acme/settings","types":"./src/index.ts"}',
        encoding="utf-8",
    )
    (tmp_path / "packages" / "settings" / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler","strict":true},'
        '"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (tmp_path / "apps" / "api" / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler","strict":true},'
        '"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (package_src / "index.ts").write_text(
        "function tenantScopedSettingsKey(tenantId: string, userId: string): string {\n"
        "  return userId;\n"
        "}\n"
        "\n"
        "function defaultSettingsKey(tenantId: string, userId: string): string {\n"
        "  return userId;\n"
        "}\n"
        "\n"
        "export { tenantScopedSettingsKey };\n"
        "export default defaultSettingsKey;\n",
        encoding="utf-8",
    )
    (app_src / "service.ts").write_text(
        'import defaultSettingsKey, { tenantScopedSettingsKey } from "@acme/settings";\n'
        "export const namedKey = tenantScopedSettingsKey('tenant-a', 'user-a');\n"
        "export const defaultKey = defaultSettingsKey('tenant-b', 'user-b');\n",
        encoding="utf-8",
    )
    diff = parse_unified_diff(
        """diff --git a/packages/settings/src/index.ts b/packages/settings/src/index.ts
index 1111111..2222222 100644
--- a/packages/settings/src/index.ts
+++ b/packages/settings/src/index.ts
@@ -1,3 +1,3 @@
 function tenantScopedSettingsKey(tenantId: string, userId: string): string {
-  return `${tenantId}:${userId}`;
+  return userId;
 }
@@ -5,3 +5,3 @@
 function defaultSettingsKey(tenantId: string, userId: string): string {
-  return `${tenantId}:${userId}`;
+  return userId;
 }
""",
        TargetMode.PATCH,
    )
    diff = classify_diff(diff, ignore_patterns=[])

    result = run_typescript_analyzer(tmp_path, diff.files)

    assert result is not None
    symbols_by_name = {symbol.name: symbol for symbol in result.files[0].changed_symbols}
    assert symbols_by_name["tenantScopedSettingsKey"].exported is True
    assert symbols_by_name["defaultSettingsKey"].exported is True
    assert any(
        reference.file == "apps/api/src/service.ts"
        and reference.kind == "call"
        and "tenantScopedSettingsKey('tenant-a', 'user-a')" in reference.text
        for reference in symbols_by_name["tenantScopedSettingsKey"].references
    )
    assert any(
        reference.file == "apps/api/src/service.ts"
        and reference.kind == "call"
        and "defaultSettingsKey('tenant-b', 'user-b')" in reference.text
        for reference in symbols_by_name["defaultSettingsKey"].references
    )


def test_typescript_analyzer_reuses_and_invalidates_repo_index_cache(
    tmp_path: Path,
    built_ts_analyzer: None,
) -> None:
    package_src = tmp_path / "packages" / "settings" / "src"
    app_src = tmp_path / "apps" / "api" / "src"
    package_src.mkdir(parents=True)
    app_src.mkdir(parents=True)
    (tmp_path / "package.json").write_text('{"workspaces":["apps/*","packages/*"]}', encoding="utf-8")
    (tmp_path / "packages" / "settings" / "package.json").write_text(
        '{"name":"@acme/settings","types":"./src/index.ts"}',
        encoding="utf-8",
    )
    (tmp_path / "packages" / "settings" / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler","strict":true},'
        '"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (package_src / "index.ts").write_text(
        "export function tenantScopedSettingsKey(tenantId: string, userId: string): string {\n  return userId;\n}\n",
        encoding="utf-8",
    )
    service = app_src / "service.ts"
    service.write_text(
        'import { tenantScopedSettingsKey } from "@acme/settings";\n'
        "export const key = tenantScopedSettingsKey('tenant-a', 'user-a');\n",
        encoding="utf-8",
    )
    diff = parse_unified_diff(
        """diff --git a/packages/settings/src/index.ts b/packages/settings/src/index.ts
index 1111111..2222222 100644
--- a/packages/settings/src/index.ts
+++ b/packages/settings/src/index.ts
@@ -1,3 +1,3 @@
 export function tenantScopedSettingsKey(tenantId: string, userId: string): string {
-  return `${tenantId}:${userId}`;
+  return userId;
 }
""",
        TargetMode.PATCH,
    )
    diff = classify_diff(diff, ignore_patterns=[])

    first = run_typescript_analyzer(tmp_path, diff.files)
    second = run_typescript_analyzer(tmp_path, diff.files)
    service.write_text(
        'import { tenantScopedSettingsKey } from "@acme/settings";\n'
        "export const key = tenantScopedSettingsKey('tenant-b', 'user-b');\n",
        encoding="utf-8",
    )
    third = run_typescript_analyzer(tmp_path, diff.files)

    assert first is not None and first.index_cache is not None
    assert Path(first.index_cache.path).exists()
    assert first.index_cache.hits == 0
    assert first.index_cache.misses == 2
    assert second is not None and second.index_cache is not None
    assert second.index_cache.files == 2
    assert second.index_cache.hits == 2
    assert second.index_cache.misses == 0
    assert second.index_cache.written is False
    assert third is not None and third.index_cache is not None
    assert third.index_cache.hits == 1
    assert third.index_cache.misses == 1
    references = third.files[0].changed_symbols[0].references
    assert any("tenantScopedSettingsKey('tenant-b', 'user-b')" in reference.text for reference in references)
    assert all("tenantScopedSettingsKey('tenant-a', 'user-a')" not in reference.text for reference in references)


def test_typescript_analyzer_can_store_repo_index_cache_outside_repo(
    tmp_path: Path,
    built_ts_analyzer: None,
) -> None:
    repo = tmp_path / "repo"
    external_cache = tmp_path / "external-cache"
    (repo / "src").mkdir(parents=True)
    (repo / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler","strict":true},'
        '"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (repo / "src" / "cache.ts").write_text(
        "export function tenantCacheKey(tenantId: string, userId: string): string {\n  return userId;\n}\n",
        encoding="utf-8",
    )
    diff = parse_unified_diff(
        """diff --git a/src/cache.ts b/src/cache.ts
index 1111111..2222222 100644
--- a/src/cache.ts
+++ b/src/cache.ts
@@ -1,3 +1,3 @@
 export function tenantCacheKey(tenantId: string, userId: string): string {
-  return `${tenantId}:${userId}`;
+  return userId;
 }
""",
        TargetMode.PATCH,
    )
    diff = classify_diff(diff, ignore_patterns=[])

    result = run_typescript_analyzer(repo, diff.files, AnalyzerConfig(index_cache_dir=str(external_cache)))
    disabled = run_typescript_analyzer(repo, diff.files, AnalyzerConfig(index_cache_enabled=False))

    assert result is not None and result.index_cache is not None
    assert Path(result.index_cache.path).parent == external_cache
    assert Path(result.index_cache.path).exists()
    assert not (repo / ".apex-ray" / "cache").exists()
    assert disabled is not None
    assert disabled.index_cache is None


def test_typescript_analyzer_default_repo_index_cache_stays_outside_repo(
    tmp_path: Path,
    monkeypatch,
    built_ts_analyzer: None,
) -> None:
    repo = tmp_path / "repo"
    cache_home = tmp_path / "cache-home"
    monkeypatch.setenv("APEX_RAY_CACHE_HOME", str(cache_home))
    (repo / "src").mkdir(parents=True)
    (repo / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler","strict":true},'
        '"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (repo / "src" / "cache.ts").write_text(
        "export function tenantCacheKey(tenantId: string, userId: string): string {\n  return userId;\n}\n",
        encoding="utf-8",
    )
    diff = parse_unified_diff(
        """diff --git a/src/cache.ts b/src/cache.ts
index 1111111..2222222 100644
--- a/src/cache.ts
+++ b/src/cache.ts
@@ -1,3 +1,3 @@
 export function tenantCacheKey(tenantId: string, userId: string): string {
-  return `${tenantId}:${userId}`;
+  return userId;
 }
""",
        TargetMode.PATCH,
    )
    diff = classify_diff(diff, ignore_patterns=[])

    result = run_typescript_analyzer(repo, diff.files)

    assert result is not None and result.index_cache is not None
    assert Path(result.index_cache.path).is_relative_to(cache_home)
    assert Path(result.index_cache.path).exists()
    assert not (repo / ".apex-ray" / "cache").exists()


def test_typescript_analyzer_finds_package_import_related_tests(
    tmp_path: Path,
    built_ts_analyzer: None,
) -> None:
    package_src = tmp_path / "packages" / "settings" / "src"
    test_dir = tmp_path / "apps" / "api" / "test"
    package_src.mkdir(parents=True)
    test_dir.mkdir(parents=True)
    (tmp_path / "package.json").write_text('{"workspaces":["apps/*","packages/*"]}', encoding="utf-8")
    (tmp_path / "packages" / "settings" / "package.json").write_text(
        '{"name":"@acme/settings","types":"./src/index.ts"}',
        encoding="utf-8",
    )
    (tmp_path / "packages" / "settings" / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler","strict":true},'
        '"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (package_src / "index.ts").write_text(
        "export function tenantScopedSettingsKey(tenantId: string, userId: string): string {\n  return userId;\n}\n",
        encoding="utf-8",
    )
    (test_dir / "settings-cache.integration.test.ts").write_text(
        'import { tenantScopedSettingsKey } from "@acme/settings";\n'
        "it('keeps tenant cache keys separate', () => {\n"
        "  expect(tenantScopedSettingsKey('tenant-a', 'user-a')).not.toEqual(\n"
        "    tenantScopedSettingsKey('tenant-b', 'user-a'),\n"
        "  );\n"
        "});\n",
        encoding="utf-8",
    )
    diff = parse_unified_diff(
        """diff --git a/packages/settings/src/index.ts b/packages/settings/src/index.ts
index 1111111..2222222 100644
--- a/packages/settings/src/index.ts
+++ b/packages/settings/src/index.ts
@@ -1,3 +1,3 @@
 export function tenantScopedSettingsKey(tenantId: string, userId: string): string {
-  return `${tenantId}:${userId}`;
+  return userId;
 }
""",
        TargetMode.PATCH,
    )
    diff = classify_diff(diff, ignore_patterns=[])

    result = run_typescript_analyzer(tmp_path, diff.files)
    packs = build_context_packs([result], diff.files, ReviewConfig(), repo_root=tmp_path) if result else []

    assert result is not None
    assert result.files[0].related_tests == ["apps/api/test/settings-cache.integration.test.ts"]
    assert len(packs) == 1
    assert packs[0].related_test_snippets[0].file == "apps/api/test/settings-cache.integration.test.ts"


def test_typescript_analyzer_filters_related_tests_with_vitest_config(
    tmp_path: Path,
    built_ts_analyzer: None,
) -> None:
    src = tmp_path / "apps" / "api" / "src"
    test_dir = tmp_path / "apps" / "api" / "test"
    src.mkdir(parents=True)
    test_dir.mkdir(parents=True)
    (tmp_path / "package.json").write_text('{"workspaces":["apps/*"]}', encoding="utf-8")
    (tmp_path / "apps" / "api" / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler","strict":true},'
        '"include":["src/**/*.ts","test/**/*.ts"]}',
        encoding="utf-8",
    )
    (tmp_path / "apps" / "api" / "vitest.config.ts").write_text(
        "import { defineConfig } from 'vitest/config';\n"
        "export default defineConfig({\n"
        "  test: {\n"
        "    include: ['src/**/*.test.ts', 'test/**/*.test.ts'],\n"
        "    exclude: [\n"
        "      'src/**/*.integration.test.ts',\n"
        "      'src/**/*.component.test.ts',\n"
        "      'test/**/*.e2e.test.ts',\n"
        "    ],\n"
        "  },\n"
        "});\n",
        encoding="utf-8",
    )
    (src / "settings.ts").write_text(
        "export function tenantSettingsKey(tenantId: string, userId: string): string {\n  return userId;\n}\n",
        encoding="utf-8",
    )
    (src / "settings.test.ts").write_text(
        'import { tenantSettingsKey } from "./settings";\n'
        "expect(tenantSettingsKey('tenant-a', 'user-a')).toBe('tenant-a:user-a');\n",
        encoding="utf-8",
    )
    (src / "settings.integration.test.ts").write_text(
        'import { tenantSettingsKey } from "./settings";\n'
        "expect(tenantSettingsKey('tenant-b', 'user-a')).toBe('tenant-b:user-a');\n",
        encoding="utf-8",
    )
    (src / "settings.component.test.ts").write_text(
        'import { tenantSettingsKey } from "./settings";\n'
        "expect(tenantSettingsKey('tenant-c', 'user-a')).toBe('tenant-c:user-a');\n",
        encoding="utf-8",
    )
    (test_dir / "settings.e2e.test.ts").write_text(
        'import { tenantSettingsKey } from "../src/settings";\n'
        "expect(tenantSettingsKey('tenant-e2e', 'user-a')).toBe('tenant-e2e:user-a');\n",
        encoding="utf-8",
    )
    diff = parse_unified_diff(
        """diff --git a/apps/api/src/settings.ts b/apps/api/src/settings.ts
index 1111111..2222222 100644
--- a/apps/api/src/settings.ts
+++ b/apps/api/src/settings.ts
@@ -1,3 +1,3 @@
 export function tenantSettingsKey(tenantId: string, userId: string): string {
-  return `${tenantId}:${userId}`;
+  return userId;
 }
""",
        TargetMode.PATCH,
    )
    diff = classify_diff(diff, ignore_patterns=[])

    result = run_typescript_analyzer(tmp_path, diff.files)

    assert result is not None
    assert result.files[0].related_tests == ["apps/api/src/settings.test.ts"]


def test_typescript_analyzer_includes_tests_from_sibling_vitest_configs(
    tmp_path: Path,
    built_ts_analyzer: None,
) -> None:
    src = tmp_path / "apps" / "api" / "src"
    src.mkdir(parents=True)
    (tmp_path / "package.json").write_text('{"workspaces":["apps/*"]}', encoding="utf-8")
    (tmp_path / "apps" / "api" / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler","strict":true},'
        '"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (tmp_path / "apps" / "api" / "vitest.config.ts").write_text(
        "import { defineConfig } from 'vitest/config';\n"
        "export default defineConfig({\n"
        "  test: {\n"
        "    include: ['src/**/*.test.ts'],\n"
        "    exclude: ['src/**/*.integration.test.ts'],\n"
        "  },\n"
        "});\n",
        encoding="utf-8",
    )
    (tmp_path / "apps" / "api" / "vitest.integration.config.ts").write_text(
        "import { defineConfig } from 'vitest/config';\n"
        "export default defineConfig({\n"
        "  test: {\n"
        "    include: ['src/**/*.integration.test.ts'],\n"
        "  },\n"
        "});\n",
        encoding="utf-8",
    )
    (src / "settings.ts").write_text(
        "export function tenantSettingsKey(tenantId: string, userId: string): string {\n  return userId;\n}\n",
        encoding="utf-8",
    )
    (src / "settings.test.ts").write_text(
        'import { tenantSettingsKey } from "./settings";\n'
        "expect(tenantSettingsKey('tenant-a', 'user-a')).toBe('tenant-a:user-a');\n",
        encoding="utf-8",
    )
    (src / "settings.integration.test.ts").write_text(
        'import { tenantSettingsKey } from "./settings";\n'
        "expect(tenantSettingsKey('tenant-b', 'user-a')).toBe('tenant-b:user-a');\n",
        encoding="utf-8",
    )
    diff = parse_unified_diff(
        """diff --git a/apps/api/src/settings.ts b/apps/api/src/settings.ts
index 1111111..2222222 100644
--- a/apps/api/src/settings.ts
+++ b/apps/api/src/settings.ts
@@ -1,3 +1,3 @@
 export function tenantSettingsKey(tenantId: string, userId: string): string {
-  return `${tenantId}:${userId}`;
+  return userId;
 }
""",
        TargetMode.PATCH,
    )
    diff = classify_diff(diff, ignore_patterns=[])

    result = run_typescript_analyzer(tmp_path, diff.files)

    assert result is not None
    assert set(result.files[0].related_tests) == {
        "apps/api/src/settings.test.ts",
        "apps/api/src/settings.integration.test.ts",
    }


def test_typescript_analyzer_prioritizes_direct_related_tests_before_import_consumers(
    tmp_path: Path,
    built_ts_analyzer: None,
) -> None:
    src = tmp_path / "apps" / "api" / "src" / "modules" / "webhook-ingestion"
    app_dir = src / "application"
    src.mkdir(parents=True)
    app_dir.mkdir(parents=True)
    (tmp_path / "package.json").write_text('{"workspaces":["apps/*"]}', encoding="utf-8")
    (tmp_path / "apps" / "api" / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler","strict":true},'
        '"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (src / "webhook-routing.service.ts").write_text(
        "const IDENTITY_FANOUT_TARGETS: ReadonlyMap<string, readonly string[]> = new Map([\n"
        "  ['applicantReviewedd', ['identity-kyc', 'identity-pep']],\n"
        "]);\n"
        "\n"
        "export class WebhookRoutingService {\n"
        "  buildJobs(eventType: string): readonly string[] {\n"
        "    return IDENTITY_FANOUT_TARGETS.get(eventType) ?? ['identity-kyc'];\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    (app_dir / "admin-webhook-ops.service.test.ts").write_text(
        "import { WebhookRoutingService } from '../webhook-routing.service.js';\n"
        "\n"
        "it('uses webhook routing during retrigger', () => {\n"
        "  expect(new WebhookRoutingService().buildJobs('applicantReviewed')).toEqual(['identity-kyc', 'identity-pep']);\n"
        "});\n",
        encoding="utf-8",
    )
    (src / "internal-webhook.controller.test.ts").write_text(
        "import { WebhookRoutingService } from './webhook-routing.service.js';\n"
        "\n"
        "it('enqueues proxy jobs from routing', () => {\n"
        "  expect(new WebhookRoutingService().buildJobs('applicantReviewed')).toEqual(['identity-kyc', 'identity-pep']);\n"
        "});\n",
        encoding="utf-8",
    )
    (src / "webhook-routing.service.test.ts").write_text(
        "import { WebhookRoutingService } from './webhook-routing.service.js';\n"
        "\n"
        "it('applicantReviewed fans out to KYC and PEP', () => {\n"
        "  expect(new WebhookRoutingService().buildJobs('applicantReviewed')).toEqual(['identity-kyc', 'identity-pep']);\n"
        "});\n",
        encoding="utf-8",
    )
    diff = parse_unified_diff(
        """diff --git a/apps/api/src/modules/webhook-ingestion/webhook-routing.service.ts b/apps/api/src/modules/webhook-ingestion/webhook-routing.service.ts
index 1111111..2222222 100644
--- a/apps/api/src/modules/webhook-ingestion/webhook-routing.service.ts
+++ b/apps/api/src/modules/webhook-ingestion/webhook-routing.service.ts
@@ -1,3 +1,3 @@
 const IDENTITY_FANOUT_TARGETS: ReadonlyMap<string, readonly string[]> = new Map([
-  ['applicantReviewed', ['identity-kyc', 'identity-pep']],
+  ['applicantReviewedd', ['identity-kyc', 'identity-pep']],
 ]);
""",
        TargetMode.PATCH,
    )
    diff = classify_diff(diff, ignore_patterns=[])

    result = run_typescript_analyzer(tmp_path, diff.files)

    assert result is not None
    assert result.files[0].related_tests[:3] == [
        "apps/api/src/modules/webhook-ingestion/webhook-routing.service.test.ts",
        "apps/api/src/modules/webhook-ingestion/internal-webhook.controller.test.ts",
        "apps/api/src/modules/webhook-ingestion/application/admin-webhook-ops.service.test.ts",
    ]


def test_related_test_snippets_anchor_on_diff_literals(
    tmp_path: Path,
    built_ts_analyzer: None,
) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (tmp_path / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler","strict":true},'
        '"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (src / "webhook-routing.ts").write_text(
        "const IDENTITY_FANOUT_TARGETS: ReadonlyMap<string, readonly string[]> = new Map([\n"
        "  ['applicantReviewedd', ['identity-kyc', 'identity-pep']],\n"
        "]);\n"
        "\n"
        "export function targetsFor(eventType: string): readonly string[] {\n"
        "  return IDENTITY_FANOUT_TARGETS.get(eventType) ?? ['identity-kyc'];\n"
        "}\n",
        encoding="utf-8",
    )
    filler = "".join(f"const fixture{index} = 'event-{index}';\n" for index in range(30))
    (src / "webhook-routing.test.ts").write_text(
        "import { expect, it } from 'vitest';\n"
        "import { targetsFor } from './webhook-routing.js';\n"
        f"{filler}\n"
        "it('fans out applicantReviewed to KYC and PEP', () => {\n"
        "  expect(targetsFor('applicantReviewed')).toEqual(['identity-kyc', 'identity-pep']);\n"
        "});\n",
        encoding="utf-8",
    )
    diff = parse_unified_diff(
        """diff --git a/src/webhook-routing.ts b/src/webhook-routing.ts
index 1111111..2222222 100644
--- a/src/webhook-routing.ts
+++ b/src/webhook-routing.ts
@@ -1,3 +1,3 @@
 const IDENTITY_FANOUT_TARGETS: ReadonlyMap<string, readonly string[]> = new Map([
-  ['applicantReviewed', ['identity-kyc', 'identity-pep']],
+  ['applicantReviewedd', ['identity-kyc', 'identity-pep']],
 ]);
""",
        TargetMode.PATCH,
    )
    diff = classify_diff(diff, ignore_patterns=[])

    result = run_typescript_analyzer(tmp_path, diff.files)
    packs = build_context_packs([result], diff.files, ReviewConfig(), repo_root=tmp_path) if result else []

    assert result is not None
    assert len(packs) == 1
    snippet = packs[0].related_test_snippets[0]
    assert snippet.file == "src/webhook-routing.test.ts"
    assert snippet.start_line >= 20
    assert "targetsFor('applicantReviewed')" in snippet.code
    assert "fixture0" not in snippet.code


def test_typescript_analyzer_finds_tests_for_reference_consumer_files(
    tmp_path: Path,
    built_ts_analyzer: None,
) -> None:
    src_dir = tmp_path / "src"
    test_dir = tmp_path / "tests"
    src_dir.mkdir()
    test_dir.mkdir()
    (tmp_path / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Node"},"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (src_dir / "auth.ts").write_text(
        "export interface User { id: string; role: 'admin' | 'member' }\n"
        "export interface Project { ownerId: string }\n"
        "export function canDeleteProject(user: User, project: Project): boolean {\n"
        "  return project.ownerId === user.id;\n"
        "}\n",
        encoding="utf-8",
    )
    (src_dir / "projects.ts").write_text(
        'import { canDeleteProject, Project, User } from "./auth";\n'
        "export function deleteProject(user: User, project: Project): string {\n"
        "  if (!canDeleteProject(user, project)) throw new Error('denied');\n"
        "  return 'deleted';\n"
        "}\n",
        encoding="utf-8",
    )
    (test_dir / "projects.test.ts").write_text(
        'import { deleteProject } from "../src/projects";\n'
        "it('allows admins to delete any project', () => {\n"
        "  expect(deleteProject({ id: 'admin-1', role: 'admin' }, { ownerId: 'owner-1' })).toBe('deleted');\n"
        "});\n",
        encoding="utf-8",
    )
    diff = parse_unified_diff(
        """diff --git a/src/auth.ts b/src/auth.ts
index 1111111..2222222 100644
--- a/src/auth.ts
+++ b/src/auth.ts
@@ -1,5 +1,5 @@
 export interface User { id: string; role: 'admin' | 'member' }
 export interface Project { ownerId: string }
 export function canDeleteProject(user: User, project: Project): boolean {
-  return user.role === 'admin' || project.ownerId === user.id;
+  return project.ownerId === user.id;
 }
""",
        TargetMode.PATCH,
    )
    diff = classify_diff(diff, ignore_patterns=[])

    result = run_typescript_analyzer(tmp_path, diff.files)
    packs = build_context_packs([result], diff.files, ReviewConfig(), repo_root=tmp_path) if result else []

    assert result is not None
    references = result.files[0].changed_symbols[0].references
    assert any(reference.file == "src/projects.ts" and reference.kind == "call" for reference in references)
    assert result.files[0].related_tests == ["tests/projects.test.ts"]
    assert len(packs) == 1
    assert packs[0].related_test_snippets[0].file == "tests/projects.test.ts"


def test_context_reference_snippets_prioritize_usages_over_imports(
    tmp_path: Path,
    built_ts_analyzer: None,
) -> None:
    package_src = tmp_path / "packages" / "settings" / "src"
    app_src = tmp_path / "apps" / "api" / "src"
    package_src.mkdir(parents=True)
    app_src.mkdir(parents=True)
    (tmp_path / "package.json").write_text('{"workspaces":["apps/*","packages/*"]}', encoding="utf-8")
    (tmp_path / "packages" / "settings" / "package.json").write_text(
        '{"name":"@acme/settings","types":"./src/index.ts"}',
        encoding="utf-8",
    )
    (tmp_path / "packages" / "settings" / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler","strict":true},'
        '"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (package_src / "index.ts").write_text(
        "export function tenantScopedSettingsKey(tenantId: string, userId: string): string {\n  return userId;\n}\n",
        encoding="utf-8",
    )
    for index in range(8):
        (app_src / f"service{index}.ts").write_text(
            'import { tenantScopedSettingsKey } from "@acme/settings";\n'
            f"export const key{index} = tenantScopedSettingsKey('tenant-{index}', 'user-a');\n",
            encoding="utf-8",
        )
    diff = parse_unified_diff(
        """diff --git a/packages/settings/src/index.ts b/packages/settings/src/index.ts
index 1111111..2222222 100644
--- a/packages/settings/src/index.ts
+++ b/packages/settings/src/index.ts
@@ -1,3 +1,3 @@
 export function tenantScopedSettingsKey(tenantId: string, userId: string): string {
-  return `${tenantId}:${userId}`;
+  return userId;
 }
""",
        TargetMode.PATCH,
    )
    diff = classify_diff(diff, ignore_patterns=[])

    result = run_typescript_analyzer(tmp_path, diff.files)
    packs = build_context_packs([result], diff.files, ReviewConfig(), repo_root=tmp_path) if result else []

    assert len(packs) == 1
    service_snippets = [
        snippet for snippet in packs[0].reference_snippets if snippet.file.startswith("apps/api/src/service")
    ]
    assert len(service_snippets) == 8
    assert all("tenantScopedSettingsKey(" in snippet.code for snippet in service_snippets)


def test_typescript_analyzer_resolves_workspace_package_exports_subpaths(
    tmp_path: Path,
    built_ts_analyzer: None,
) -> None:
    package_src = tmp_path / "packages" / "audit" / "src" / "events"
    app_src = tmp_path / "apps" / "api" / "src"
    package_src.mkdir(parents=True)
    app_src.mkdir(parents=True)
    (tmp_path / "package.json").write_text('{"workspaces":["apps/*","packages/*"]}', encoding="utf-8")
    (tmp_path / "packages" / "audit" / "package.json").write_text(
        '{"name":"@acme/audit","exports":{"./audit-event":{"types":"./src/events/audit-event.ts",'
        '"default":"./dist/events/audit-event.js"}}}',
        encoding="utf-8",
    )
    (tmp_path / "packages" / "audit" / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler","strict":true},'
        '"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (package_src / "audit-event.ts").write_text(
        "export function buildAuditEvent(tenantId: string, action: string): string {\n  return action;\n}\n",
        encoding="utf-8",
    )
    (app_src / "audit.ts").write_text(
        'import { buildAuditEvent } from "@acme/audit/audit-event";\n'
        "export const event = buildAuditEvent('tenant-a', 'delete-project');\n",
        encoding="utf-8",
    )
    diff = parse_unified_diff(
        """diff --git a/packages/audit/src/events/audit-event.ts b/packages/audit/src/events/audit-event.ts
index 1111111..2222222 100644
--- a/packages/audit/src/events/audit-event.ts
+++ b/packages/audit/src/events/audit-event.ts
@@ -1,3 +1,3 @@
 export function buildAuditEvent(tenantId: string, action: string): string {
-  return `${tenantId}:${action}`;
+  return action;
 }
""",
        TargetMode.PATCH,
    )
    diff = classify_diff(diff, ignore_patterns=[])

    result = run_typescript_analyzer(tmp_path, diff.files)

    assert result is not None
    references = result.files[0].changed_symbols[0].references
    assert any(
        reference.file == "apps/api/src/audit.ts"
        and reference.kind == "call"
        and "buildAuditEvent('tenant-a', 'delete-project')" in reference.text
        for reference in references
    )


def test_typescript_analyzer_resolves_workspace_package_wildcard_exports(
    tmp_path: Path,
    built_ts_analyzer: None,
) -> None:
    package_src = tmp_path / "packages" / "audit" / "src" / "events"
    app_src = tmp_path / "apps" / "api" / "src"
    package_src.mkdir(parents=True)
    app_src.mkdir(parents=True)
    (tmp_path / "package.json").write_text('{"workspaces":["apps/*","packages/*"]}', encoding="utf-8")
    (tmp_path / "packages" / "audit" / "package.json").write_text(
        '{"name":"@acme/audit","exports":{"./events/*":{"types":"./src/events/*.ts"}}}',
        encoding="utf-8",
    )
    (tmp_path / "packages" / "audit" / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"ESNext","moduleResolution":"Bundler","strict":true},'
        '"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (package_src / "audit-event.ts").write_text(
        "export function buildAuditEvent(tenantId: string, action: string): string {\n  return action;\n}\n",
        encoding="utf-8",
    )
    (app_src / "audit.ts").write_text(
        'import { buildAuditEvent } from "@acme/audit/events/audit-event";\n'
        "export const event = buildAuditEvent('tenant-a', 'delete-project');\n",
        encoding="utf-8",
    )
    diff = parse_unified_diff(
        """diff --git a/packages/audit/src/events/audit-event.ts b/packages/audit/src/events/audit-event.ts
index 1111111..2222222 100644
--- a/packages/audit/src/events/audit-event.ts
+++ b/packages/audit/src/events/audit-event.ts
@@ -1,3 +1,3 @@
 export function buildAuditEvent(tenantId: string, action: string): string {
-  return `${tenantId}:${action}`;
+  return action;
 }
""",
        TargetMode.PATCH,
    )
    diff = classify_diff(diff, ignore_patterns=[])

    result = run_typescript_analyzer(tmp_path, diff.files)

    assert result is not None
    references = result.files[0].changed_symbols[0].references
    assert any(reference.file == "apps/api/src/audit.ts" and reference.kind == "call" for reference in references)


def test_typescript_analyzer_resolves_nodenext_js_specifiers_to_ts_sources(
    tmp_path: Path,
    built_ts_analyzer: None,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022","module":"NodeNext","moduleResolution":"NodeNext","strict":true},'
        '"include":["src/**/*.ts"]}',
        encoding="utf-8",
    )
    (tmp_path / "src" / "cache.ts").write_text(
        "export function tenantCacheKey(tenantId: string, userId: string): string {\n  return userId;\n}\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "service.ts").write_text(
        "import { tenantCacheKey } from \"./cache.js\";\nexport const key = tenantCacheKey('tenant-a', 'user-a');\n",
        encoding="utf-8",
    )
    diff = parse_unified_diff(
        """diff --git a/src/cache.ts b/src/cache.ts
index 1111111..2222222 100644
--- a/src/cache.ts
+++ b/src/cache.ts
@@ -1,3 +1,3 @@
 export function tenantCacheKey(tenantId: string, userId: string): string {
-  return `${tenantId}:${userId}`;
+  return userId;
 }
""",
        TargetMode.PATCH,
    )
    diff = classify_diff(diff, ignore_patterns=[])

    result = run_typescript_analyzer(tmp_path, diff.files)

    assert result is not None
    references = result.files[0].changed_symbols[0].references
    assert any(
        reference.file == "src/service.ts"
        and reference.kind == "call"
        and "tenantCacheKey('tenant-a', 'user-a')" in reference.text
        for reference in references
    )


def test_small_related_file_changes_cluster_symbols(built_ts_analyzer: None) -> None:
    diff = parse_unified_diff((TS_FIXTURE / "routes.diff").read_text(encoding="utf-8"), TargetMode.PATCH)
    diff = classify_diff(diff, ignore_patterns=[])

    result = run_typescript_analyzer(TS_FIXTURE, diff.files)
    packs = build_context_packs([result], diff.files, ReviewConfig(), repo_root=TS_FIXTURE) if result else []

    assert len(packs) == 1
    assert packs[0].id == "src/routes.ts#cluster:middleware+config"
    assert packs[0].symbol is not None
    assert packs[0].symbol.name == "middleware"
    assert [symbol.name for symbol in packs[0].symbols] == ["middleware", "config"]
    assert len(packs[0].changed_snippets) == 2
    assert [snippet.file for snippet in packs[0].changed_snippets] == ["src/routes.ts", "src/routes.ts"]
    assert 'request.path.startsWith("/api/webhook/")' in packs[0].changed_snippets[0].code
    assert 'matcher: ["/api/webhook/:path*"]' in packs[0].changed_snippets[1].code
    assert len(packs[0].diff_snippet) > 10


def test_many_related_file_changes_cluster_symbols(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    source = tmp_path / "src" / "chart.ts"
    source.write_text(
        "\n".join(
            [
                "export const tokenPattern = /[a-z]/;",
                "export const colorPattern = /#[0-9a-f]+/;",
                "export function sanitizeToken(value: string): string { return value; }",
                "export function sanitizeColor(value: string): string { return value; }",
                "export function buildChartCss(value: string): string { return value; }",
                "",
            ]
        ),
        encoding="utf-8",
    )
    diff = parse_unified_diff(
        """diff --git a/src/chart.ts b/src/chart.ts
index 1111111..2222222 100644
--- a/src/chart.ts
+++ b/src/chart.ts
@@ -1,5 +1,5 @@
-export const tokenPattern = /.*/;
-export const colorPattern = /.*/;
-export function sanitizeToken(value: string): string { return value; }
-export function sanitizeColor(value: string): string { return value; }
-export function buildChartCss(value: string): string { return value; }
+export const tokenPattern = /[a-z]/;
+export const colorPattern = /#[0-9a-f]+/;
+export function sanitizeToken(value: string): string { return value; }
+export function sanitizeColor(value: string): string { return value; }
+export function buildChartCss(value: string): string { return value; }
""",
        TargetMode.PATCH,
    )
    diff = classify_diff(diff, ignore_patterns=[])
    symbols = [
        AnalyzerSymbol(name="tokenPattern", kind="variable", startLine=1, endLine=1),
        AnalyzerSymbol(name="colorPattern", kind="variable", startLine=2, endLine=2),
        AnalyzerSymbol(name="sanitizeToken", kind="function", startLine=3, endLine=3),
        AnalyzerSymbol(name="sanitizeColor", kind="function", startLine=4, endLine=4),
        AnalyzerSymbol(name="buildChartCss", kind="function", startLine=5, endLine=5),
    ]
    result = AnalyzerResult(
        language="typescript",
        projectRoot=str(tmp_path),
        files=[
            AnalyzerFile(
                path="src/chart.ts",
                symbols=symbols,
                changedSymbols=symbols,
            )
        ],
    )

    packs = build_context_packs([result], diff.files, ReviewConfig(), repo_root=tmp_path)

    assert len(packs) == 1
    assert packs[0].id == ("src/chart.ts#cluster:tokenPattern+colorPattern+sanitizeToken+sanitizeColor+buildChartCss")
    assert [symbol.name for symbol in packs[0].symbols] == [
        "tokenPattern",
        "colorPattern",
        "sanitizeToken",
        "sanitizeColor",
        "buildChartCss",
    ]
    assert len(packs[0].changed_snippets) == 5


def test_context_pack_drops_redundant_parent_class_symbol(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    source = tmp_path / "src" / "service.ts"
    source.write_text(
        "\n".join(
            [
                "export class AuthService {",
                "  login(): string {",
                "    const method = 'sms';",
                "    return method;",
                "  }",
                "}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    diff = parse_unified_diff(
        """diff --git a/src/service.ts b/src/service.ts
index 1111111..2222222 100644
--- a/src/service.ts
+++ b/src/service.ts
@@ -1,6 +1,6 @@
 export class AuthService {
   login(): string {
-    const method = 'totp';
+    const method = 'sms';
     return method;
   }
 }
""",
        TargetMode.PATCH,
    )
    diff = classify_diff(diff, ignore_patterns=[])
    result = AnalyzerResult(
        language="typescript",
        projectRoot=str(tmp_path),
        files=[
            AnalyzerFile(
                path="src/service.ts",
                symbols=[
                    AnalyzerSymbol(name="AuthService", kind="class", startLine=1, endLine=6),
                    AnalyzerSymbol(name="login", kind="method", startLine=2, endLine=5),
                ],
                changedSymbols=[
                    AnalyzerSymbol(name="AuthService", kind="class", startLine=1, endLine=6),
                    AnalyzerSymbol(name="login", kind="method", startLine=2, endLine=5),
                ],
            )
        ],
    )

    packs = build_context_packs([result], diff.files, ReviewConfig(), repo_root=tmp_path)

    assert len(packs) == 1
    assert packs[0].id == "src/service.ts#login:1"
    assert [symbol.name for symbol in packs[0].symbols] == ["login"]


def test_large_changed_symbol_sets_are_chunked_with_scoped_diffs(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "flags.ts").write_text(
        "".join(f"export const value{index} = {index + 1};\n" for index in range(30)),
        encoding="utf-8",
    )
    diff_text = "diff --git a/src/flags.ts b/src/flags.ts\n--- a/src/flags.ts\n+++ b/src/flags.ts\n"
    for index in range(30):
        line = index + 1
        diff_text += (
            f"@@ -{line},1 +{line},1 @@\n"
            f"-export const value{index} = {index};\n"
            f"+export const value{index} = {index + 1};\n"
        )
    diff = classify_diff(parse_unified_diff(diff_text, TargetMode.PATCH), ignore_patterns=[])
    result = AnalyzerResult(
        language="typescript",
        projectRoot=str(tmp_path),
        files=[
            AnalyzerFile(
                path="src/flags.ts",
                changedSymbols=[
                    AnalyzerSymbol(
                        name=f"value{index}",
                        kind="variable",
                        startLine=index + 1,
                        endLine=index + 1,
                        exported=True,
                    )
                    for index in range(30)
                ],
            )
        ],
    )

    packs = build_context_packs([result], diff.files, ReviewConfig(), repo_root=tmp_path)

    assert len(packs) == 3
    assert all(len(pack.symbols) <= 12 for pack in packs)
    assert "+export const value0 = 1;" in packs[0].diff_snippet
    assert "+export const value29 = 30;" not in packs[0].diff_snippet
    assert "+export const value29 = 30;" in packs[-1].diff_snippet


def test_negative_refactor_fixture_still_builds_context_pack(built_ts_analyzer: None) -> None:
    diff = parse_unified_diff((TS_FIXTURE / "cart_refactor.diff").read_text(encoding="utf-8"), TargetMode.PATCH)
    diff = classify_diff(diff, ignore_patterns=[])

    result = run_typescript_analyzer(TS_FIXTURE, diff.files)
    packs = build_context_packs([result], diff.files, ReviewConfig(), repo_root=TS_FIXTURE) if result else []

    assert len(packs) == 1
    assert packs[0].symbol is not None
    assert packs[0].symbol.name == "calculateTotal"
    assert "+  const total = items.reduce((sum, item) => sum + item.price * item.quantity, 0);" in packs[0].diff_snippet


def test_file_level_pack_uses_hunk_changed_snippets() -> None:
    diff = parse_unified_diff((TS_FIXTURE / "routes.diff").read_text(encoding="utf-8"), TargetMode.PATCH)
    diff = classify_diff(diff, ignore_patterns=[])
    result = AnalyzerResult(
        language="typescript",
        projectRoot=str(TS_FIXTURE),
        files=[AnalyzerFile(path="src/routes.ts")],
    )

    packs = build_context_packs([result], diff.files, ReviewConfig(), repo_root=TS_FIXTURE)

    assert len(packs) == 1
    assert packs[0].id == "src/routes.ts#file"
    assert packs[0].symbol is None
    assert packs[0].impact_notes[0].startswith("Changed scope: file-level change")
    assert packs[0].changed_snippets
    assert 'request.path.startsWith("/api/webhook/")' in packs[0].changed_snippets[0].code


def test_context_pack_budget_truncates_large_snippets(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    source_lines = [
        "export function bigThing(): number {\n",
        *[f"  const value{i} = {i};\n" for i in range(80)],
        "  return value79;\n",
        "}\n",
    ]
    (tmp_path / "src" / "big.ts").write_text("".join(source_lines), encoding="utf-8")
    (tmp_path / "src" / "use.ts").write_text(
        "import { bigThing } from './big';\n"
        "export const result = bigThing();\n" + "".join(f"export const useLine{i} = {i};\n" for i in range(50)),
        encoding="utf-8",
    )
    (tmp_path / "tests" / "big.test.ts").write_text(
        "import { bigThing } from '../src/big';\n"
        + "".join(f"it('case {i}', () => expect(bigThing()).toBeGreaterThan({i}));\n" for i in range(50)),
        encoding="utf-8",
    )
    diff = parse_unified_diff(
        """diff --git a/src/big.ts b/src/big.ts
index 1111111..2222222 100644
--- a/src/big.ts
+++ b/src/big.ts
@@ -1,4 +1,4 @@
 export function bigThing(): number {
-  return 0;
+  return value79;
 }
""",
        TargetMode.PATCH,
    )
    diff = classify_diff(diff, ignore_patterns=[])
    result = AnalyzerResult(
        language="typescript",
        projectRoot=str(tmp_path),
        files=[
            AnalyzerFile(
                path="src/big.ts",
                relatedTests=["tests/big.test.ts"],
                changedSymbols=[
                    AnalyzerSymbol(
                        name="bigThing",
                        kind="function",
                        startLine=1,
                        endLine=len(source_lines),
                        exported=True,
                        references=[
                            AnalyzerReference(
                                file="src/use.ts",
                                line=2,
                                text="export const result = bigThing();",
                            )
                        ],
                    )
                ],
            )
        ],
    )
    config = ReviewConfig(
        context=ContextConfig(
            max_changed_snippet_lines=120,
            max_related_test_snippet_lines=80,
            max_pack_chars=1900,
        )
    )

    packs = build_context_packs([result], diff.files, config, repo_root=tmp_path)

    assert len(packs) == 1
    pack = packs[0]
    assert pack.stats.truncated is True
    assert pack.stats.truncation_notes
    assert pack.stats.estimated_chars <= config.context.max_pack_chars
    assert pack.stats.changed_snippet_lines < len(source_lines)
    assert "+  return value79;" in pack.diff_snippet


def test_context_pack_budget_truncates_reference_snippet_before_dropping_it(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "big.ts").write_text(
        "export function bigThing(): number {\n  return 1;\n}\n",
        encoding="utf-8",
    )
    reference_lines = [
        "import { bigThing } from './big';\n",
        *[f"export const before{i} = {i};\n" for i in range(80)],
        "export const result = bigThing();\n",
        *[f"export const after{i} = {i};\n" for i in range(80)],
    ]
    (tmp_path / "src" / "use.ts").write_text("".join(reference_lines), encoding="utf-8")
    diff = parse_unified_diff(
        """diff --git a/src/big.ts b/src/big.ts
index 1111111..2222222 100644
--- a/src/big.ts
+++ b/src/big.ts
@@ -1,3 +1,3 @@
 export function bigThing(): number {
-  return 0;
+  return 1;
 }
""",
        TargetMode.PATCH,
    )
    diff = classify_diff(diff, ignore_patterns=[])
    result = AnalyzerResult(
        language="typescript",
        projectRoot=str(tmp_path),
        files=[
            AnalyzerFile(
                path="src/big.ts",
                changedSymbols=[
                    AnalyzerSymbol(
                        name="bigThing",
                        kind="function",
                        startLine=1,
                        endLine=3,
                        exported=True,
                        references=[
                            AnalyzerReference(
                                file="src/use.ts",
                                line=82,
                                text="export const result = bigThing();",
                                kind="call",
                            )
                        ],
                    )
                ],
            )
        ],
    )
    config = ReviewConfig(
        context=ContextConfig(
            reference_snippet_context_lines=80,
            max_pack_chars=2200,
        )
    )

    packs = build_context_packs([result], diff.files, config, repo_root=tmp_path)

    assert len(packs) == 1
    pack = packs[0]
    assert pack.stats.truncated is True
    assert pack.stats.estimated_chars <= config.context.max_pack_chars
    assert pack.reference_snippets
    assert "export const result = bigThing();" in pack.reference_snippets[0].code
    assert len(pack.reference_snippets[0].code.splitlines()) < len(reference_lines)


def test_context_pack_budget_preserves_metadata_before_references(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "controller.ts").write_text(
        "@Post(':id/retry')\n"
        "@RequirePermission(Permission.RETRY)\n"
        "export function retryTransfer(id: string): string {\n"
        "  return id;\n"
        "}\n",
        encoding="utf-8",
    )
    reference_lines = [
        "import { retryTransfer } from './controller';\n",
        *[f"export const before{i} = {i};\n" for i in range(120)],
        "export const result = retryTransfer('id');\n",
        *[f"export const after{i} = {i};\n" for i in range(120)],
    ]
    (tmp_path / "src" / "consumer.ts").write_text("".join(reference_lines), encoding="utf-8")
    diff = parse_unified_diff(
        """diff --git a/src/controller.ts b/src/controller.ts
index 1111111..2222222 100644
--- a/src/controller.ts
+++ b/src/controller.ts
@@ -1,5 +1,5 @@
 @Post(':id/retry')
 @RequirePermission(Permission.RETRY)
 export function retryTransfer(id: string): string {
-  return `${id}:retry`;
+  return id;
 }
""",
        TargetMode.PATCH,
    )
    diff = classify_diff(diff, ignore_patterns=[])
    result = AnalyzerResult(
        language="typescript",
        projectRoot=str(tmp_path),
        files=[
            AnalyzerFile(
                path="src/controller.ts",
                changedSymbols=[
                    AnalyzerSymbol(
                        name="retryTransfer",
                        kind="function",
                        startLine=1,
                        endLine=5,
                        exported=True,
                        references=[
                            AnalyzerReference(
                                file="src/consumer.ts",
                                line=122,
                                text="export const result = retryTransfer('id');",
                                kind="call",
                            )
                        ],
                        metadata=[
                            AnalyzerReference(
                                file="src/controller.ts",
                                line=1,
                                text="@Post(':id/retry')",
                                kind="metadata",
                            ),
                            AnalyzerReference(
                                file="src/controller.ts",
                                line=2,
                                text="@RequirePermission(Permission.RETRY)",
                                kind="metadata",
                            ),
                        ],
                    )
                ],
            )
        ],
    )
    config = ReviewConfig(
        context=ContextConfig(
            reference_snippet_context_lines=120,
            max_pack_chars=3100,
        )
    )

    packs = build_context_packs([result], diff.files, config, repo_root=tmp_path)

    assert len(packs) == 1
    pack = packs[0]
    assert pack.stats.truncated is True
    assert pack.stats.estimated_chars <= config.context.max_pack_chars
    assert pack.metadata_snippets
    assert any("@RequirePermission(Permission.RETRY)" in snippet.code for snippet in pack.metadata_snippets)
    assert any("reference snippet" in note for note in pack.stats.truncation_notes)
    assert pack.reference_snippets
    assert all(len(snippet.code.splitlines()) < 241 for snippet in pack.reference_snippets)


def test_context_pack_budget_preserves_contracts_before_low_value_context() -> None:
    contract_code = (
        "export interface SettlementContract {\n"
        + "".join(f"  field{index}: string;\n" for index in range(70))
        + "  criticalTailGuarantee: 'preserved';\n"
        + "}\n"
    )
    reference_code = "".join(f"export const consumer{index} = {index};\n" for index in range(30))
    test_code = "".join(f"it('case {index}', () => expect(true).toBe(true));\n" for index in range(30))
    pack = ContextPack(
        id="src/settlement.ts#file",
        file="src/settlement.ts",
        diff_snippet=["- return oldState;", "+ return newState;"],
        changed_snippets=[
            CodeSnippet(
                file="src/settlement.ts",
                start_line=10,
                end_line=12,
                code="export function settle(): string {\n  return newState;\n}\n",
            )
        ],
        contract_snippets=[
            CodeSnippet(
                file="src/contracts.ts",
                start_line=1,
                end_line=73,
                code=contract_code,
            )
        ],
        reference_snippets=[
            CodeSnippet(
                file="src/consumer.ts",
                start_line=1,
                end_line=30,
                code=reference_code,
            )
        ],
        related_test_snippets=[
            CodeSnippet(
                file="tests/settlement.test.ts",
                start_line=1,
                end_line=30,
                code=test_code,
            )
        ],
    )
    pack_without_low_value_context = pack.model_copy(
        deep=True,
        update={"reference_snippets": [], "related_test_snippets": []},
    )
    budget = _estimated_pack_chars(pack_without_low_value_context) + 80
    config = ContextConfig(max_pack_chars=budget)

    finalized = _finalize_pack(pack, config)

    assert finalized.stats.estimated_chars <= budget
    assert finalized.contract_snippets
    assert "criticalTailGuarantee" in finalized.contract_snippets[0].code
    assert not any("contract snippet" in note for note in finalized.stats.truncation_notes)
    assert any(
        "related test snippet" in note or "reference snippet" in note for note in finalized.stats.truncation_notes
    )


def test_context_pack_budget_compacts_graph_payload_when_snippets_cannot_fit() -> None:
    huge_reference = AnalyzerReference(
        file="src/consumer.ts",
        line=1,
        text="callWithHugePayload(" + ("x" * 6000) + ")",
        kind="call",
    )
    symbol = AnalyzerSymbol(
        name="settle",
        kind="function",
        startLine=10,
        endLine=12,
        exported=True,
        signature="(): string",
        references=[huge_reference],
        callees=[huge_reference],
        contracts=[huge_reference],
        metadata=[huge_reference],
    )
    pack = ContextPack(
        id="src/settlement.ts#settle:1",
        file="src/settlement.ts",
        diff_snippet=["- return oldState;", "+ return newState;"],
        changed_snippets=[
            CodeSnippet(
                file="src/settlement.ts",
                start_line=10,
                end_line=12,
                code="export function settle(): string {\n  return newState;\n}\n",
            )
        ],
        symbol=symbol,
        symbols=[symbol],
        references=[huge_reference],
        callees=[huge_reference],
        contracts=[huge_reference],
        metadata=[huge_reference],
    )
    config = ContextConfig(max_pack_chars=1800)

    finalized = _finalize_pack(pack, config)

    assert finalized.stats.estimated_chars <= config.max_pack_chars
    assert finalized.references == []
    assert finalized.callees == []
    assert finalized.contracts == []
    assert finalized.metadata == []
    assert finalized.symbol is not None
    assert finalized.symbol.references == []
    assert finalized.symbol.callees == []
    assert finalized.symbol.contracts == []
    assert finalized.symbol.metadata == []
    assert any("compacted over-budget analyzer graph" in note for note in finalized.stats.truncation_notes)


def test_context_pack_budget_compacts_graph_before_truncating_changed_snippet() -> None:
    huge_reference = AnalyzerReference(
        file="src/consumer.ts",
        line=1,
        text="callWithHugePayload(" + ("x" * 6000) + ")",
        kind="call",
    )
    symbol = AnalyzerSymbol(
        name="settle",
        kind="function",
        startLine=10,
        endLine=30,
        references=[huge_reference],
    )
    changed_code = (
        "export function settle(): string {\n"
        + "".join(f"  const value{index} = '{index}';\n" for index in range(12))
        + "  return value11;\n"
        + "}\n"
    )
    pack = ContextPack(
        id="src/settlement.ts#settle:1",
        file="src/settlement.ts",
        diff_snippet=["- return oldState;", "+ return newState;"],
        changed_snippets=[CodeSnippet(file="src/settlement.ts", start_line=10, end_line=24, code=changed_code)],
        symbol=symbol,
        symbols=[symbol],
        references=[huge_reference],
    )
    compacted = pack.model_copy(
        deep=True,
        update={
            "symbol": symbol.model_copy(update={"references": []}),
            "symbols": [symbol.model_copy(update={"references": []})],
            "references": [],
        },
    )
    config = ContextConfig(max_pack_chars=_estimated_pack_chars(compacted) + 20)

    finalized = _finalize_pack(pack, config)

    assert finalized.stats.estimated_chars <= config.max_pack_chars
    assert finalized.changed_snippets[0].code == changed_code
    assert any("compacted over-budget analyzer graph" in note for note in finalized.stats.truncation_notes)


def test_context_pack_risk_signals_are_localized_to_symbol_ranges() -> None:
    changed_file = ChangedFile(
        old_path="src/payments.ts",
        new_path="src/payments.ts",
        file_kind=FileKind.SOURCE,
        risk_signals=[
            RiskSignal(
                kind="auth", severity=RiskSeverity.HIGH, reason="Auth changed.", file="src/payments.ts", line=12
            ),
            RiskSignal(
                kind="cache", severity=RiskSeverity.LOW, reason="Cache changed.", file="src/payments.ts", line=90
            ),
            RiskSignal(kind="test_gap", severity=RiskSeverity.LOW, reason="No tests.", file="src/payments.ts"),
        ],
    )
    symbol = AnalyzerSymbol(
        name="authorizePayment",
        kind="function",
        startLine=10,
        endLine=20,
    )

    localized = _risk_signals_for_symbols(changed_file, [symbol])

    assert [signal.kind for signal in localized] == ["auth"]


def test_symbolized_source_pack_retains_file_level_risk_once() -> None:
    diff = parse_unified_diff(
        """diff --git a/src/payments.ts b/src/payments.ts
index 1111111..2222222 100644
--- a/src/payments.ts
+++ b/src/payments.ts
@@ -1,3 +1,3 @@
 export function authorizePayment(): boolean {
-  return false;
+  return true;
 }
""",
        TargetMode.PATCH,
    )
    diff = classify_diff(diff, ignore_patterns=[])
    result = AnalyzerResult(
        language="typescript",
        projectRoot="/repo",
        files=[
            AnalyzerFile(
                path="src/payments.ts",
                changedSymbols=[
                    AnalyzerSymbol(
                        name="authorizePayment",
                        kind="function",
                        startLine=1,
                        endLine=3,
                    )
                ],
            )
        ],
    )

    packs = build_context_packs([result], diff.files, ReviewConfig())

    assert len(packs) == 1
    assert any(signal.kind == "test_gap" and signal.line is None for signal in packs[0].risk_signals)


def test_context_policy_changes_cache_key(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "big.ts").write_text(
        "export function bigThing(): number {\n  return 1;\n}\n",
        encoding="utf-8",
    )
    diff = parse_unified_diff(
        """diff --git a/src/big.ts b/src/big.ts
index 1111111..2222222 100644
--- a/src/big.ts
+++ b/src/big.ts
@@ -1,3 +1,3 @@
 export function bigThing(): number {
-  return 0;
+  return 1;
 }
""",
        TargetMode.PATCH,
    )
    diff = classify_diff(diff, ignore_patterns=[])
    result = AnalyzerResult(
        language="typescript",
        projectRoot=str(tmp_path),
        files=[
            AnalyzerFile(
                path="src/big.ts",
                changedSymbols=[
                    AnalyzerSymbol(
                        name="bigThing",
                        kind="function",
                        startLine=1,
                        endLine=3,
                        exported=True,
                    )
                ],
            )
        ],
    )

    default_pack = build_context_packs([result], diff.files, ReviewConfig(), repo_root=tmp_path)[0]
    tuned_pack = build_context_packs(
        [result],
        diff.files,
        ReviewConfig(context=ContextConfig(max_pack_chars=ReviewConfig().context.max_pack_chars - 1)),
        repo_root=tmp_path,
    )[0]
    llm_config = LLMConfig(provider=LLMProviderName.FAKE)

    assert default_pack.stats.policy_key != tuned_pack.stats.policy_key
    assert review_cache_key(default_pack, llm_config) != review_cache_key(tuned_pack, llm_config)
