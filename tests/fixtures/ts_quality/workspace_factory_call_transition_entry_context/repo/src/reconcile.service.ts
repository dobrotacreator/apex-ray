import { OrderEvent, OrderStatus, transitionOrder } from './order-status.js';

export function reconcileOrder(): OrderStatus {
  const result = transitionOrder(
    OrderStatus.AWAITING_RECONCILIATION,
    OrderEvent.OPS_RECONCILED_LANDED_CANCEL,
  );
  if (!result.ok) throw new Error('invalid transition');
  return result.value;
}
