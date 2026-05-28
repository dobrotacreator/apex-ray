import { VaToVaAgentHandler } from './handlers/va-to-va-agent.handler.js';
import { FLOW_HANDLER_PORT } from './tokens.js';

export const FLOW_HANDLER_PROVIDERS = [
  {
    provide: FLOW_HANDLER_PORT,
    useFactory: (handler: VaToVaAgentHandler) => [handler],
    inject: [VaToVaAgentHandler],
  },
];
