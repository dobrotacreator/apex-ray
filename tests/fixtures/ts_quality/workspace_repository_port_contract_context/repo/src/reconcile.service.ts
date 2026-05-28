import type { SettlementStep, SettlementStepRepository, TransactionHandle } from './settlement-step-repository.port.js';
export class ReconcileService {
  constructor(private readonly stepRepo: SettlementStepRepository) {}
  async findSteps(orderId: string, trx: TransactionHandle): Promise<ReadonlyArray<SettlementStep>> {
    return this.stepRepo.findAmbiguousReverseSteps(orderId, undefined);
  }
}
