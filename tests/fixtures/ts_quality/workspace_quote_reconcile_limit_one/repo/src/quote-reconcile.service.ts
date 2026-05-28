import type { AuditRepository } from './audit-repository.port.js';
import type { SettlementStep, SettlementStepRepository } from './settlement-step-repository.port.js';
import { transitionOrder, type OrderStatus } from './status.js';

export interface ReconcileRequest {
  outcome: 'LANDED' | 'LOST';
  externalRef?: string;
  cancellationRequested: boolean;
}

export interface ReconcileResult {
  orderStatus: OrderStatus;
  resolvedStepCount: number;
}

export class QuoteReconcileService {
  constructor(
    private readonly stepRepo: SettlementStepRepository,
    private readonly auditRepo: AuditRepository,
  ) {}

  async reconcile(orderId: string, request: ReconcileRequest): Promise<ReconcileResult> {
    const ambiguousSteps = await this.stepRepo.findAmbiguousReverseStepsForUpdate(orderId);
    if (ambiguousSteps.length === 0) {
      throw new Error('order has no ambiguous reverse steps');
    }

    const nextOrderStatus = transitionOrder(request.outcome, request.cancellationRequested);

    if (request.outcome === 'LANDED') {
      await this.applyLandedSteps({
        steps: ambiguousSteps.slice(0, 1),
        externalRef: request.externalRef,
      });
      await this.assertNoRemainingAmbiguousSteps(orderId);
    } else {
      await this.recordLostStepAudits({ steps: ambiguousSteps.slice(0, 1) });
    }

    await this.auditRepo.record('ORDER_RECONCILED', orderId, {
      resolved_step_count: ambiguousSteps.length,
      outcome: request.outcome,
    });
    return { orderStatus: nextOrderStatus, resolvedStepCount: ambiguousSteps.length };
  }

  private async applyLandedSteps(args: {
    steps: ReadonlyArray<SettlementStep>;
    externalRef: string | undefined;
  }): Promise<void> {
    for (const step of args.steps) {
      await this.stepRepo.transitionToReversed(step.id, args.externalRef);
      await this.auditRepo.record('STEP_RECONCILED_TO_REVERSED', step.id, {
        previous_status: step.status,
      });
    }
  }

  private async recordLostStepAudits(args: { steps: ReadonlyArray<SettlementStep> }): Promise<void> {
    for (const step of args.steps) {
      await this.auditRepo.record('STEP_RECONCILED_REVERSE_LOST', step.id, {
        previous_status: step.status,
      });
    }
  }

  private async assertNoRemainingAmbiguousSteps(orderId: string): Promise<void> {
    const remaining = await this.stepRepo.findAmbiguousReverseStepsForUpdate(orderId);
    if (remaining.length > 0) {
      throw new Error(`ambiguous reverse steps still remain: ${remaining.map((s) => s.id).join(',')}`);
    }
  }
}
