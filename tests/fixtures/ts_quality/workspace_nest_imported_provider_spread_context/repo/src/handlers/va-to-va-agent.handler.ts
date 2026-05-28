import type { FlowHandler } from '../flow-handler.port.js';
export class VaToVaAgentHandler implements FlowHandler {
  supports(route: string): boolean {
    return true;
  }
}
