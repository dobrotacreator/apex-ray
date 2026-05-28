import type { Order, SettlementStep } from './domain.js';
import type { ProbeReverseOutcome, StepExecutorPort } from './step-executor.port.js';

export type ReverseProbeDecision = 'REVERSED' | 'REVERSE_FAILED' | 'WAITING';

export function decideAfterFailedReverseProbe(
  executor: StepExecutorPort,
  args: { step: SettlementStep; order: Order },
): ReverseProbeDecision {
  const outcome: ProbeReverseOutcome = executor.probeReverse(args);
  if (outcome.status === 'COMPLETED') return 'REVERSED';
  if (outcome.status === 'PENDING') return 'WAITING';
  return 'REVERSE_FAILED';
}
