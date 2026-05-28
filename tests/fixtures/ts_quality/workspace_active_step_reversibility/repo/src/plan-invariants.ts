import { ACTIVE_STEP_TYPES, REVERSIBLE_STEP_TYPES, StepType, TRANSFER_STEP_TYPES } from './step-type.js';

export const PlanInvariantViolation = {
  MISSING_EXECUTOR: 'MISSING_EXECUTOR',
  REVERSIBILITY_MISMATCH: 'REVERSIBILITY_MISMATCH',
  SCREEN_BEFORE_CREDIT_VIOLATED: 'SCREEN_BEFORE_CREDIT_VIOLATED',
  UNSUPPORTED_STEP_TYPE_IN_INITIAL_IMPL: 'UNSUPPORTED_STEP_TYPE_IN_INITIAL_IMPL',
} as const;

export type PlanInvariantViolation = (typeof PlanInvariantViolation)[keyof typeof PlanInvariantViolation];

export interface SettlementStep {
  stepIndex: number;
  stepType: StepType;
  reversible: boolean;
  targetsClientAccount: boolean;
  screened: boolean;
}

export interface PlanValidationContext {
  registeredExecutorStepTypes: ReadonlySet<string>;
}

const ACTIVE_STEP_TYPE_SET: ReadonlySet<string> = new Set(ACTIVE_STEP_TYPES);

const SCREEN_GATE_STEP_TYPES: ReadonlySet<string> = new Set(
  [...TRANSFER_STEP_TYPES].filter((stepType) => ACTIVE_STEP_TYPE_SET.has(stepType)),
);

export function validatePlan(
  steps: ReadonlyArray<SettlementStep>,
  ctx: PlanValidationContext,
): PlanInvariantViolation | null {
  for (const step of steps) {
    if (!ACTIVE_STEP_TYPE_SET.has(step.stepType)) {
      return PlanInvariantViolation.UNSUPPORTED_STEP_TYPE_IN_INITIAL_IMPL;
    }
    const shouldBeReversible = REVERSIBLE_STEP_TYPES.has(step.stepType);
    if (step.reversible !== shouldBeReversible) {
      return PlanInvariantViolation.REVERSIBILITY_MISMATCH;
    }
    if (!ctx.registeredExecutorStepTypes.has(step.stepType)) {
      return PlanInvariantViolation.MISSING_EXECUTOR;
    }
    if (SCREEN_GATE_STEP_TYPES.has(step.stepType) && step.targetsClientAccount && !step.screened) {
      return PlanInvariantViolation.SCREEN_BEFORE_CREDIT_VIOLATED;
    }
  }
  return null;
}
