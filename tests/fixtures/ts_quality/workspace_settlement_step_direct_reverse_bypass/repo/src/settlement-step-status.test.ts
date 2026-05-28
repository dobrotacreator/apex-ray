import { InvalidStateTransitionError } from './state-machine.js';
import { StepEvent, StepStatus, transitionSettlementStep } from './settlement-step-status.js';

describe('transitionSettlementStep compensate edges', () => {
  it('routes completed steps through REVERSING before a successful reverse settles', () => {
    const claim = transitionSettlementStep(StepStatus.COMPLETED, StepEvent.COMPENSATE);
    expect(claim.ok && claim.value).toBe(StepStatus.REVERSING);

    const settle = transitionSettlementStep(StepStatus.REVERSING, StepEvent.COMPENSATE_OK);
    expect(settle.ok && settle.value).toBe(StepStatus.REVERSED);
  });

  it('rejects the direct COMPLETED -> REVERSED bypass that the intermediate REVERSING state closes', () => {
    const result = transitionSettlementStep(StepStatus.COMPLETED, StepEvent.COMPENSATE_OK);
    expect(result.ok).toBe(false);
    if (result.ok) throw new Error('expected invalid transition');
    expect(result.error).toBeInstanceOf(InvalidStateTransitionError);
  });

  it('rejects the direct COMPLETED -> REVERSE_FAILED bypass', () => {
    const result = transitionSettlementStep(StepStatus.COMPLETED, StepEvent.COMPENSATE_FAILED);
    expect(result.ok).toBe(false);
  });
});
