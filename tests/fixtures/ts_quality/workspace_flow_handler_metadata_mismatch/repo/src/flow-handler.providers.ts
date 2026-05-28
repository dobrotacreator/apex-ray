import { VaToFiatAgentHandler } from './handlers/va-to-fiat-agent.handler.js';
import { VaToVaAgentHandler } from './handlers/va-to-va-agent.handler.js';
import { FLOW_HANDLER_PORT } from './flow-handler.port.js';

export const FLOW_HANDLER_PROVIDERS = [
  VaToFiatAgentHandler,
  VaToVaAgentHandler,
  {
    provide: FLOW_HANDLER_PORT,
    useFactory: (vaToFiat: VaToFiatAgentHandler, vaToVa: VaToVaAgentHandler) => [vaToFiat, vaToVa],
    inject: [VaToFiatAgentHandler, VaToVaAgentHandler],
  },
];
