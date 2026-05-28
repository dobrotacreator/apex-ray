import { VaToVaAgentHandler } from './handlers/va-to-va-agent.handler.js';
import { FlowHandlerRegistry } from './flow-handler.registry.js';
import { FLOW_HANDLER_PORT } from './tokens.js';

function Module(_metadata: unknown): ClassDecorator {
  return () => undefined;
}

@Module({
  providers: [
    VaToVaAgentHandler,
    {
      provide: FLOW_HANDLER_PORT,
      useFactory: (handler: VaToVaAgentHandler) => [handler],
      inject: [VaToVaAgentHandler],
    },
    FlowHandlerRegistry,
  ],
  exports: [FlowHandlerRegistry],
})
export class QuoteModule {}
