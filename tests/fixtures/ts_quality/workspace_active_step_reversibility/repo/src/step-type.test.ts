import { describe, expect, it } from 'vitest';
import { ACTIVE_STEP_TYPES, assertActiveIsSubsetOfReversible, REVERSIBLE_STEP_TYPES, StepType } from './step-type.js';

describe('StepType', () => {
  it('ACTIVE_STEP_TYPES contains exactly the Plan-0 active set', () => {
    expect([...ACTIVE_STEP_TYPES].sort()).toEqual([
      'INTERNAL_BANK_TRANSFER',
      'INTERNAL_VAULT_TRANSFER',
      'LAYER_2_SCREENING',
      'OPERATOR_CONFIRMATION',
    ]);
  });

  it('REVERSIBLE_STEP_TYPES excludes all point-of-no-return step types', () => {
    expect(REVERSIBLE_STEP_TYPES.has(StepType.EXTERNAL_CRYPTO_SEND)).toBe(false);
    expect(REVERSIBLE_STEP_TYPES.has(StepType.EXTERNAL_FIAT_SEND)).toBe(false);
    expect(REVERSIBLE_STEP_TYPES.has(StepType.EXTERNAL_TRAVEL_RULE_EXCHANGE)).toBe(false);
    expect(REVERSIBLE_STEP_TYPES.has(StepType.INTERNAL_VAULT_TRANSFER)).toBe(true);
  });

  it('ACTIVE_STEP_TYPES is a subset of REVERSIBLE_STEP_TYPES', () => {
    for (const stepType of ACTIVE_STEP_TYPES) {
      expect(REVERSIBLE_STEP_TYPES.has(stepType)).toBe(true);
    }
  });
});

describe('assertActiveIsSubsetOfReversible', () => {
  it('does not throw when the invariant holds for the current set definitions', () => {
    expect(() => assertActiveIsSubsetOfReversible()).not.toThrow();
  });
});
