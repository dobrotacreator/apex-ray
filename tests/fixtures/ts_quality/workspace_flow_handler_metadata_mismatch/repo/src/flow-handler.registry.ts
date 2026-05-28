import type { ExecutionMode, FlowType, HandlerVersion } from './domain.js';
import type { FlowHandler } from './flow-handler.port.js';
import { FLOW_HANDLER_PORT } from './flow-handler.port.js';

function Inject(_token: unknown): ParameterDecorator {
  return () => undefined;
}

export class FlowHandlerRegistry {
  private readonly handlers: ReadonlyArray<FlowHandler>;

  constructor(@Inject(FLOW_HANDLER_PORT) handlers: ReadonlyArray<FlowHandler>) {
    const seen = new Set<string>();
    for (const handler of handlers) {
      const key = `${handler.flowType}:${handler.executionMode}:${handler.version}`;
      if (seen.has(key)) {
        throw new Error(`Duplicate FlowHandler for ${key}`);
      }
      seen.add(key);
    }
    this.handlers = handlers;
  }

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
