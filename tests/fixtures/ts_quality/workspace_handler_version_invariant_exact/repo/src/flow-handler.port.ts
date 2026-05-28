import type { ExecutionMode, FlowType, HandlerVersion } from './domain.js';

export interface FlowHandler {
  readonly flowType: FlowType;
  readonly executionMode: ExecutionMode;
  readonly version: HandlerVersion;
}
