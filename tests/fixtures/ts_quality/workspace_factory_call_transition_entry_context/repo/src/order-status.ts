export const OrderStatus = {
  AWAITING_RECONCILIATION: 'AWAITING_RECONCILIATION',
  FAILED: 'FAILED',
  CANCELLED: 'CANCELLED',
} as const;
export type OrderStatus = (typeof OrderStatus)[keyof typeof OrderStatus];

export const OrderEvent = {
  OPS_RECONCILED_LANDED_CANCEL: 'OPS_RECONCILED_LANDED_CANCEL',
} as const;
export type OrderEvent = (typeof OrderEvent)[keyof typeof OrderEvent];

function createStateMachine<S extends string, E extends string>(
  _name: string,
  transitions: ReadonlyArray<readonly [S, E, S]>,
): (from: S, event: E) => { ok: true; value: S } | { ok: false } {
  return (from, event) => {
    const match = transitions.find(([candidateFrom, candidateEvent]) => candidateFrom === from && candidateEvent === event);
    return match ? { ok: true, value: match[2] } : { ok: false };
  };
}
export const transitionOrder = createStateMachine<OrderStatus, OrderEvent>('order', [
  [OrderStatus.AWAITING_RECONCILIATION, OrderEvent.OPS_RECONCILED_LANDED_CANCEL, OrderStatus.FAILED],
]);
