import { ExecutionMode, FlowType } from './domain.js';
import { handlerVersion } from './handler-version.js';
import type { FlowHandler } from './flow-handler.port.js';
import { FlowHandlerRegistry } from './flow-handler.registry.js';

it('resolveLatest compares semver numerically', () => {
  const low: FlowHandler = {
    flowType: FlowType.VA_TO_FIAT,
    executionMode: ExecutionMode.AGENT,
    version: handlerVersion('2.0.0'),
  };
  const high: FlowHandler = { ...low, version: handlerVersion('10.0.0') };
  const registry = new FlowHandlerRegistry([low, high]);

  expect(registry.resolveLatest({ flowType: FlowType.VA_TO_FIAT, executionMode: ExecutionMode.AGENT })).toBe(high);
});
