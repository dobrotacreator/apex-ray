import type { PlanHandlerBinding } from './settlement-plan-repository.port.js';
import { FlowHandlerRegistry } from './flow-handler.registry.js';

export class HandlerVersionInvariantService {
  static findOrphans(
    triples: ReadonlyArray<PlanHandlerBinding>,
    registry: FlowHandlerRegistry,
  ): ReadonlyArray<PlanHandlerBinding> {
    return triples.filter(
      (triple) =>
        registry.resolveLatest({
          flowType: triple.flowType,
          executionMode: triple.executionMode,
        }) === null,
    );
  }
}
