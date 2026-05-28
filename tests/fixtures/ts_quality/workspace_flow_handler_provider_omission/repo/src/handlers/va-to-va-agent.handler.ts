import { ExecutionMode, FlowType, handlerVersion, type HandlerVersion } from '../domain.js';
import type { FlowHandler } from '../flow-handler.port.js';

export class VaToVaAgentHandler implements FlowHandler {
  readonly flowType = FlowType.VA_TO_VA;
  readonly executionMode = ExecutionMode.AGENT;
  readonly version: HandlerVersion = handlerVersion('1.0.0');

  buildPlan(orderId: string): string[] {
    return [`va:${orderId}`];
  }
}
