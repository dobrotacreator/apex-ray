import type { ExecutionMode, FlowType } from './domain.js';
import type { HandlerVersion } from './handler-version.js';

export interface FlowHandler {
  readonly flowType: FlowType;
  readonly executionMode: ExecutionMode;
  readonly version: HandlerVersion;
}
