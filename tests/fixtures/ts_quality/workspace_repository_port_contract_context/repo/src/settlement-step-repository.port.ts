export interface TransactionHandle {
  readonly id: string;
}

export interface SettlementStep {
  readonly id: string;
  readonly orderId: string;
}

export interface SettlementStepRepository {
  /**
   * Must run under the caller's transaction handle and row locks.
   * Must return all ambiguous reverse steps for the order; do not LIMIT 1,
   * because reconciliation must resolve every ambiguous reverse leg together.
   */
  findAmbiguousReverseSteps(
    orderId: string,
    trx: TransactionHandle,
  ): Promise<ReadonlyArray<SettlementStep>>;
}
