import type { ExecutionMode, FlowType, HandlerVersion } from './domain.js';

export interface PlanHandlerBinding {
  flowType: FlowType;
  executionMode: ExecutionMode;
  /** Must resolve exactly; falling back to latest can strand active persisted plans. */
  handlerVersion: HandlerVersion;
}

export interface SettlementPlanRepository {
  /**
   * Returns distinct `(flowType, executionMode, handlerVersion)` triples for
   * ACTIVE and COMPENSATING plans. Startup must fail when any triple cannot be
   * resolved exactly; falling back to a newer handler version can strand or
   * replay a persisted saga with incompatible plan semantics.
   */
  findNonTerminalPlanHandlers(): Promise<ReadonlyArray<PlanHandlerBinding>>;
}
