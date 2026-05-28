import { FLOW_HANDLER_PROVIDERS } from './flow-handler.providers.js';
import { FlowHandlerRegistry } from './flow-handler.registry.js';

function Module(_metadata: unknown): ClassDecorator {
  return () => undefined;
}

@Module({
  providers: [
    ...FLOW_HANDLER_PROVIDERS,
    FlowHandlerRegistry,
  ],
  exports: [FlowHandlerRegistry],
})
export class QuoteModule {}
