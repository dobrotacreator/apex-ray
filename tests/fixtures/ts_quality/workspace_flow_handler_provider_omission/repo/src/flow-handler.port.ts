import type { ExecutionMode, FlowType, HandlerVersion } from './domain.js';

export const FLOW_HANDLER_PORT = Symbol('FLOW_HANDLER_PORT');

export interface FlowHandler {
  readonly flowType: FlowType;
  readonly executionMode: ExecutionMode;
  readonly version: HandlerVersion;
  buildPlan(orderId: string): string[];
}
