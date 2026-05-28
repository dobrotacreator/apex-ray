export const StepStatus = {
  REVERSE_FAILED: 'REVERSE_FAILED',
  AWAITING_REVERSE_EXTERNAL: 'AWAITING_REVERSE_EXTERNAL',
  REVERSED: 'REVERSED',
} as const;
export type StepStatus = (typeof StepStatus)[keyof typeof StepStatus];

export const OrderStatus = {
  AWAITING_RECONCILIATION: 'AWAITING_RECONCILIATION',
  FAILED: 'FAILED',
  CANCELLED: 'CANCELLED',
} as const;
export type OrderStatus = (typeof OrderStatus)[keyof typeof OrderStatus];

export function transitionOrder(outcome: 'LANDED' | 'LOST', cancellationRequested: boolean): OrderStatus {
  if (outcome === 'LOST') return OrderStatus.FAILED;
  return cancellationRequested ? OrderStatus.CANCELLED : OrderStatus.FAILED;
}
