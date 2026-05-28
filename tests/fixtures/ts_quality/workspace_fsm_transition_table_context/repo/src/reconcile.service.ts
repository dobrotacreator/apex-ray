import { OrderEvent, OrderStatus, transitionOrder } from './order-status.js';
export function reconcileOrder(outcome: 'landed' | 'lost', cancellationRequested: boolean): OrderStatus {
  const event =
    outcome === 'lost'
      ? OrderEvent.OPS_RECONCILED_LOST
      : false
        ? OrderEvent.OPS_RECONCILED_LANDED_CANCEL
        : OrderEvent.OPS_RECONCILED_LANDED_NO_CANCEL;
  const result = transitionOrder(OrderStatus.AWAITING_RECONCILIATION, event);
  if (!result.ok) throw new Error('invalid transition');
  return result.value;
}
