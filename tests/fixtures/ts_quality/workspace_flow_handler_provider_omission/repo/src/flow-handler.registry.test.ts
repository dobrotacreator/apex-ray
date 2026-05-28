import { ExecutionMode, FlowType } from './domain.js';
import { FLOW_HANDLER_PROVIDERS } from './flow-handler.providers.js';
import { FlowHandlerRegistry } from './flow-handler.registry.js';
import { VaToFiatAgentHandler } from './handlers/va-to-fiat-agent.handler.js';
import { VaToVaAgentHandler } from './handlers/va-to-va-agent.handler.js';

it('registers every flow handler under FLOW_HANDLER_PORT', () => {
  const vaToFiat = new VaToFiatAgentHandler();
  const vaToVa = new VaToVaAgentHandler();
  const factoryProvider = FLOW_HANDLER_PROVIDERS.find(
    (provider): provider is { useFactory: (...handlers: [VaToFiatAgentHandler, VaToVaAgentHandler]) => unknown[] } =>
      typeof provider === 'object' && 'useFactory' in provider,
  );
  const registry = new FlowHandlerRegistry(factoryProvider?.useFactory(vaToFiat, vaToVa) ?? []);

  expect(
    registry.resolve({ flowType: FlowType.VA_TO_VA, executionMode: ExecutionMode.AGENT, version: '1.0.0' }),
  ).toBe(vaToVa);
});
