import { calculateProxyDeliveryStatus, type ProxyDelivery } from './proxy-delivery-status.calculator.js';
import { WebhookRoutingService } from './webhook-routing.service.js';

export class AdminWebhookOpsService {
  constructor(private readonly routing: WebhookRoutingService = new WebhookRoutingService()) {}

  expectedTargetsFor(eventType: string): readonly string[] {
    return this.routing.buildJobs(eventType).map((job) => job.target);
  }

  deliveryStatus(eventType: string, deliveries: readonly ProxyDelivery[]): string {
    const expectedTargets = this.routing.buildJobs(eventType).map((job) => job.target);
    return calculateProxyDeliveryStatus(deliveries, expectedTargets);
  }
}
