import { describe, expect, it } from 'vitest';
import { StepType, type Order, type SettlementStep } from './domain.js';
import { Layer2ScreeningExecutor } from './layer-2-screening.executor.js';
import { decideAfterFailedReverseProbe } from './plan-driven-settlement-orchestrator.js';

const order: Order = { id: 'order-1' };
const step: SettlementStep = {
  id: 'step-1',
  stepType: StepType.LAYER_2_SCREENING,
  externalId: null,
};

describe('Layer2ScreeningExecutor probeReverse', () => {
  it('returns NOT_FOUND because screening has no external reverse to probe', () => {
    const executor = new Layer2ScreeningExecutor();

    expect(executor.probeReverse({ step, order })).toEqual({ status: 'NOT_FOUND' });
  });

  it('keeps the failed reverse path when the no-op reverse cannot be probed', () => {
    const executor = new Layer2ScreeningExecutor();

    expect(decideAfterFailedReverseProbe(executor, { step, order })).toBe('REVERSE_FAILED');
  });
});
