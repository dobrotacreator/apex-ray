import { describe, expect, it } from 'vitest';
import { PlanInvariantViolation, validatePlan } from './plan-invariants.js';
import { StepType } from './step-type.js';

describe('validatePlan screen-before-credit gate', () => {
  it('applies the rule to INTERNAL_BANK_TRANSFER credits too', () => {
    const result = validatePlan([
      {
        stepIndex: 0,
        stepType: StepType.INTERNAL_BANK_TRANSFER,
        targetsClientAccount: true,
        screened: false,
      },
    ]);

    expect(result).toBe(PlanInvariantViolation.SCREEN_BEFORE_CREDIT_VIOLATED);
  });

  it('applies the rule to INTERNAL_VAULT_TRANSFER credits', () => {
    const result = validatePlan([
      {
        stepIndex: 0,
        stepType: StepType.INTERNAL_VAULT_TRANSFER,
        targetsClientAccount: true,
        screened: false,
      },
    ]);

    expect(result).toBe(PlanInvariantViolation.SCREEN_BEFORE_CREDIT_VIOLATED);
  });
});
