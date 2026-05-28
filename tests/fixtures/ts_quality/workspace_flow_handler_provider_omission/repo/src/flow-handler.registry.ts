import type { ExecutionMode, FlowType, HandlerVersion } from './domain.js';
import type { FlowHandler } from './flow-handler.port.js';
import { FLOW_HANDLER_PORT } from './flow-handler.port.js';

function Inject(_token: unknown): ParameterDecorator {
  return () => undefined;
}

export class FlowHandlerRegistry {
  constructor(@Inject(FLOW_HANDLER_PORT) private readonly handlers: ReadonlyArray<FlowHandler>) {}

  resolve(key: { flowType: FlowType; executionMode: ExecutionMode; version: HandlerVersion }): FlowHandler | null {
    return (
      this.handlers.find(
        (handler) =>
          handler.flowType === key.flowType &&
          handler.executionMode === key.executionMode &&
          handler.version === key.version,
      ) ?? null
    );
  }
}
