import type { StepStatus } from './status.js';

export interface SettlementStep {
  readonly id: string;
  readonly orderId: string;
  readonly status: StepStatus;
  readonly externalId?: string;
}

export interface SettlementStepRepository {
  /**
   * Returns ALL ambiguous-reverse steps for the order, not just the first one.
   * Multi-step compensation can leave several siblings in REVERSE_FAILED or
   * AWAITING_REVERSE_EXTERNAL, and the operator's reconcile decision must
   * drain the whole set under one outcome.
   */
  findAmbiguousReverseStepsForUpdate(orderId: string): Promise<SettlementStep[]>;

  transitionToReversed(stepId: string, externalRef: string | undefined): Promise<void>;
}
