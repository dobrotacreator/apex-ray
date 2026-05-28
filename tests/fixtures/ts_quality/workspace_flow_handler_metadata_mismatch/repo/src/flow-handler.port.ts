import type { ExecutionMode, FlowType, HandlerVersion } from './domain.js';

export const FLOW_HANDLER_PORT = Symbol('FLOW_HANDLER_PORT');

export interface FlowHandler {
  readonly flowType: FlowType;
  readonly executionMode: ExecutionMode;
  readonly version: HandlerVersion;

  /**
   * The registry resolves handlers by the exact `(flowType, executionMode, version)`
   * tuple. Changing one metadata field changes which persisted plans can use
   * this handler.
   */
  buildPlan(orderId: string): string[];
}
