import type { ExecutionMode, FlowType, HandlerVersion } from './domain.js';
import type { FlowHandler } from './flow-handler.port.js';

export class FlowHandlerRegistry {
  constructor(private readonly handlers: ReadonlyArray<FlowHandler>) {}

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

  resolveLatest(key: { flowType: FlowType; executionMode: ExecutionMode }): FlowHandler | null {
    return (
      this.handlers.find(
        (handler) => handler.flowType === key.flowType && handler.executionMode === key.executionMode,
      ) ?? null
    );
  }
}
