export type ProxyDeliveryStatus = 'PENDING' | 'DELIVERED';

export interface ProxyDelivery {
  target: string;
  delivered: boolean;
}

export function calculateProxyDeliveryStatus(
  deliveries: readonly ProxyDelivery[],
  expectedTargets: readonly string[],
): ProxyDeliveryStatus {
  return expectedTargets.every((target) =>
    deliveries.some((delivery) => delivery.target === target && delivery.delivered),
  )
    ? 'DELIVERED'
    : 'PENDING';
}
