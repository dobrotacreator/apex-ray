import { ExecutionMode, FlowType, handlerVersion } from './domain.js';
import type { FlowHandler } from './flow-handler.port.js';
import type { PlanHandlerBinding } from './settlement-plan-repository.port.js';
import { FlowHandlerRegistry } from './flow-handler.registry.js';
import { HandlerVersionInvariantService } from './handler-version-invariant.service.js';

it('treats an active plan on a removed old handler version as an orphan', () => {
  const registry = new FlowHandlerRegistry([makeHandler('2.0.0')]);
  const orphan = makeBinding('1.0.0');

  expect(HandlerVersionInvariantService.findOrphans([orphan], registry)).toEqual([orphan]);
});

function makeHandler(version: string): FlowHandler {
  return {
    flowType: FlowType.VA_TO_FIAT,
    executionMode: ExecutionMode.AGENT,
    version: handlerVersion(version),
  };
}

function makeBinding(version: string): PlanHandlerBinding {
  return {
    flowType: FlowType.VA_TO_FIAT,
    executionMode: ExecutionMode.AGENT,
    handlerVersion: handlerVersion(version),
  };
}
