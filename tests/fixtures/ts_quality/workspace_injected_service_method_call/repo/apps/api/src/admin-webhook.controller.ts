import { Inject } from "@nestjs/common";
import type { AdminWebhookOpsService } from "@acme/webhooks";
import { ADMIN_WEBHOOK_OPS_SERVICE } from "./tokens";

export class AdminWebhookController {
  constructor(@Inject(ADMIN_WEBHOOK_OPS_SERVICE) private readonly ops: AdminWebhookOpsService) {}

  retrigger(id: string, actorId: string): string {
    return this.ops.retrigger(id, actorId);
  }
}
