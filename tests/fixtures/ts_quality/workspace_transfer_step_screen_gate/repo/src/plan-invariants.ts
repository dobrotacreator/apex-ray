import { ACTIVE_STEP_TYPES, StepType, TRANSFER_STEP_TYPES } from './step-type.js';

export const PlanInvariantViolation = {
  SCREEN_BEFORE_CREDIT_VIOLATED: 'SCREEN_BEFORE_CREDIT_VIOLATED',
} as const;

export type PlanInvariantViolation = (typeof PlanInvariantViolation)[keyof typeof PlanInvariantViolation];

export interface SettlementStep {
  stepIndex: number;
  stepType: StepType;
  targetsClientAccount: boolean;
  screened: boolean;
}

const ACTIVE_STEP_TYPE_SET: ReadonlySet<string> = new Set(ACTIVE_STEP_TYPES);

/**
 * Screen-before-credit applies to every fund-moving step type that handlers
 * are allowed to emit. Removing an active transfer type from TRANSFER_STEP_TYPES
 * silently bypasses this gate for that rail.
 */
const SCREEN_GATE_STEP_TYPES: ReadonlySet<string> = new Set(
  [...TRANSFER_STEP_TYPES].filter((stepType) => ACTIVE_STEP_TYPE_SET.has(stepType)),
);

export function validatePlan(steps: ReadonlyArray<SettlementStep>): PlanInvariantViolation | null {
  for (const step of steps) {
    if (!SCREEN_GATE_STEP_TYPES.has(step.stepType)) continue;
    if (step.targetsClientAccount && !step.screened) {
      return PlanInvariantViolation.SCREEN_BEFORE_CREDIT_VIOLATED;
    }
  }
  return null;
}
