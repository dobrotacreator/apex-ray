import type { ExecutionMode, FlowType } from './domain.js';
import { compareHandlerVersions, type HandlerVersion } from './handler-version.js';
import type { FlowHandler } from './flow-handler.port.js';

export class FlowHandlerRegistry {
  constructor(private readonly handlers: ReadonlyArray<FlowHandler>) {}

  resolveLatest(key: { flowType: FlowType; executionMode: ExecutionMode }): FlowHandler | null {
    const matches = this.handlers.filter(
      (handler) => handler.flowType === key.flowType && handler.executionMode === key.executionMode,
    );
    if (matches.length === 0) return null;
    return matches.reduce((left, right) =>
      compareHandlerVersions(left.version, right.version as HandlerVersion) >= 0 ? left : right,
    );
  }
}
