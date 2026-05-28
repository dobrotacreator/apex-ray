import { StepEvent, StepStatus, transitionSettlementStep } from './settlement-step-status.js';

export function claimCompletedStepForReverse(): StepStatus {
  const result = transitionSettlementStep(StepStatus.COMPLETED, StepEvent.COMPENSATE);
  if (!result.ok) throw result.error;
  return result.value;
}

export function settleClaimedReverseSuccess(): StepStatus {
  const result = transitionSettlementStep(StepStatus.REVERSING, StepEvent.COMPENSATE_OK);
  if (!result.ok) throw result.error;
  return result.value;
}
