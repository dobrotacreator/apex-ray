import { QuoteReconcileService } from './quote-reconcile.service.js';
import type { AuditRepository } from './audit-repository.port.js';
import type { SettlementStep, SettlementStepRepository } from './settlement-step-repository.port.js';
import { OrderStatus, StepStatus } from './status.js';

describe('QuoteReconcileService', () => {
  it('drains ALL ambiguous reverse steps when there are >1 (LANDED, multi-step)', async () => {
    const h = buildHarness([
      { id: 'step-a', orderId: 'order-1', status: StepStatus.REVERSE_FAILED },
      { id: 'step-b', orderId: 'order-1', status: StepStatus.REVERSE_FAILED },
    ]);

    const result = await h.service.reconcile('order-1', {
      outcome: 'LANDED',
      externalRef: 'CR-MULTI',
      cancellationRequested: false,
    });

    expect(result.orderStatus).toBe(OrderStatus.FAILED);
    expect(result.resolvedStepCount).toBe(2);
    expect(h.stepRepo.steps.get('step-a')?.status).toBe(StepStatus.REVERSED);
    expect(h.stepRepo.steps.get('step-b')?.status).toBe(StepStatus.REVERSED);
    const stepAudits = h.auditRepo.recorded.filter((row) => row.action === 'STEP_RECONCILED_TO_REVERSED');
    expect(stepAudits.map((row) => row.entityId).sort()).toEqual(['step-a', 'step-b']);
  });

  it('drains ALL ambiguous reverse steps when there are >1 (LOST, multi-step)', async () => {
    const h = buildHarness([
      { id: 'step-a', orderId: 'order-1', status: StepStatus.REVERSE_FAILED },
      { id: 'step-b', orderId: 'order-1', status: StepStatus.AWAITING_REVERSE_EXTERNAL },
    ]);

    const result = await h.service.reconcile('order-1', {
      outcome: 'LOST',
      cancellationRequested: false,
    });

    expect(result.orderStatus).toBe(OrderStatus.FAILED);
    expect(result.resolvedStepCount).toBe(2);
    const lostAudits = h.auditRepo.recorded.filter((row) => row.action === 'STEP_RECONCILED_REVERSE_LOST');
    expect(lostAudits.map((row) => row.entityId).sort()).toEqual(['step-a', 'step-b']);
  });
});

function buildHarness(steps: SettlementStep[]): {
  service: QuoteReconcileService;
  stepRepo: SettlementStepRepository & { steps: Map<string, SettlementStep> };
  auditRepo: AuditRepository & { recorded: Array<{ action: string; entityId: string }> };
} {
  const stepMap = new Map(steps.map((step) => [step.id, step]));
  const stepRepo = {
    steps: stepMap,
    async findAmbiguousReverseStepsForUpdate(orderId: string): Promise<SettlementStep[]> {
      return [...stepMap.values()].filter(
        (step) =>
          step.orderId === orderId &&
          (step.status === StepStatus.REVERSE_FAILED || step.status === StepStatus.AWAITING_REVERSE_EXTERNAL),
      );
    },
    async transitionToReversed(stepId: string, externalRef: string | undefined): Promise<void> {
      const step = stepMap.get(stepId);
      if (!step) throw new Error(`missing step ${stepId}`);
      stepMap.set(stepId, { ...step, status: StepStatus.REVERSED, externalId: externalRef });
    },
  };
  const auditRepo = {
    recorded: [] as Array<{ action: string; entityId: string }>,
    async record(action: string, entityId: string): Promise<void> {
      this.recorded.push({ action, entityId });
    },
  };
  return { service: new QuoteReconcileService(stepRepo, auditRepo), stepRepo, auditRepo };
}
