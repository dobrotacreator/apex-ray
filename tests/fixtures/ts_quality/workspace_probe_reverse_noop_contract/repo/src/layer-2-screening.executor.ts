import { StepType, type Order, type SettlementStep } from './domain.js';
import type { ProbeReverseOutcome, ReverseOutcome, StepExecutorPort } from './step-executor.port.js';

export class Layer2ScreeningExecutor implements StepExecutorPort {
  readonly stepType = StepType.LAYER_2_SCREENING;

  reverse(_args: { step: SettlementStep; order: Order }): ReverseOutcome {
    return { kind: 'NOOP' };
  }

  probeReverse(_args: { step: SettlementStep; order: Order }): ProbeReverseOutcome {
    return { status: 'COMPLETED', externalId: 'screening-noop' };
  }
}
