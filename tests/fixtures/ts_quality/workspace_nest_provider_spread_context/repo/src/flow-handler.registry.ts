import { FLOW_HANDLER_PORT } from './tokens.js';
import type { FlowHandler } from './flow-handler.port.js';

function Inject(_token: unknown): ParameterDecorator {
  return () => undefined;
}

export class FlowHandlerRegistry {
  constructor(@Inject(FLOW_HANDLER_PORT) private readonly handlers: ReadonlyArray<FlowHandler>) {}
}
