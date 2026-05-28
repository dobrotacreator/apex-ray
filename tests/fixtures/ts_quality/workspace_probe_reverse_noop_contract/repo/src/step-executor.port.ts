import type { Order, SettlementStep, StepType } from './domain.js';

export type ReverseOutcome =
  | { kind: 'REVERSED'; externalId: string }
  | { kind: 'NOOP' };

export type ProbeReverseOutcome =
  | { status: 'COMPLETED'; externalId: string }
  | { status: 'PENDING' }
  | { status: 'DECLINED' }
  | { status: 'NOT_FOUND' };

export interface StepExecutorPort {
  readonly stepType: StepType;
  reverse(args: { step: SettlementStep; order: Order }): ReverseOutcome;
  /**
   * Probe the external system before terminalising a failed reverse.
   *
   * Executors that have no external reverse to probe, such as screening and
   * operator gates, MUST return `{ status: 'NOT_FOUND' }` so the orchestrator
   * preserves the failed reverse path. Returning COMPLETED tells the
   * orchestrator that a compensating transfer landed and marks the step
   * REVERSED with the supplied external id.
   */
  probeReverse(args: { step: SettlementStep; order: Order }): ProbeReverseOutcome;
}
