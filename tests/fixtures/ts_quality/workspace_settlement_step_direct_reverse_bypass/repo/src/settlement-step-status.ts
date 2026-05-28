import { createStateMachine } from './state-machine.js';

export const StepStatus = {
  PLANNED: 'PLANNED',
  DISPATCHED: 'DISPATCHED',
  COMPLETED: 'COMPLETED',
  FAILED: 'FAILED',
  REVERSING: 'REVERSING',
  REVERSED: 'REVERSED',
  REVERSE_FAILED: 'REVERSE_FAILED',
} as const;

export type StepStatus = (typeof StepStatus)[keyof typeof StepStatus];

export const StepEvent = {
  DISPATCH: 'DISPATCH',
  SYNC_OK: 'SYNC_OK',
  DISPATCH_ERROR: 'DISPATCH_ERROR',
  COMPENSATE: 'COMPENSATE',
  COMPENSATE_OK: 'COMPENSATE_OK',
  COMPENSATE_FAILED: 'COMPENSATE_FAILED',
  RETRY: 'RETRY',
} as const;

export type StepEvent = (typeof StepEvent)[keyof typeof StepEvent];

export const transitionSettlementStep = createStateMachine<StepStatus, StepEvent>('settlement_step', [
  [StepStatus.PLANNED, StepEvent.DISPATCH, StepStatus.DISPATCHED],
  [StepStatus.DISPATCHED, StepEvent.SYNC_OK, StepStatus.COMPLETED],
  [StepStatus.DISPATCHED, StepEvent.DISPATCH_ERROR, StepStatus.FAILED],
  // Two-phase CAS compensate pattern. Phase 1 claims COMPLETED -> REVERSING;
  // phase 2 settles from REVERSING. There must be no direct
  // COMPLETED -> REVERSED or COMPLETED -> REVERSE_FAILED edge.
  [StepStatus.COMPLETED, StepEvent.COMPENSATE, StepStatus.REVERSING],
  [StepStatus.COMPLETED, StepEvent.COMPENSATE_OK, StepStatus.REVERSED],
  [StepStatus.REVERSING, StepEvent.COMPENSATE_FAILED, StepStatus.REVERSE_FAILED],
  [StepStatus.REVERSING, StepEvent.RETRY, StepStatus.COMPLETED],
]);
