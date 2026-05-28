import { VaToVaAgentHandler } from './handlers/va-to-va-agent.handler.js';
import { FlowHandlerRegistry } from './flow-handler.registry.js';
import { FLOW_HANDLER_PORT } from './tokens.js';

function Module(_metadata: unknown): ClassDecorator {
  return () => undefined;
}

const FLOW_HANDLER_PROVIDERS = [
  {
    provide: FLOW_HANDLER_PORT,
    useFactory: (handler: VaToVaAgentHandler) => [handler],
    inject: [VaToVaAgentHandler],
  },
];

@Module({
  providers: [
    ...FLOW_HANDLER_PROVIDERS,
    FlowHandlerRegistry,
  ],
  exports: [FlowHandlerRegistry],
})
export class QuoteModule {}
