import { AdminWebhookOpsService } from "@acme/webhooks";
import { ADMIN_WEBHOOK_OPS_SERVICE } from "./tokens";

export const providers = [
  {
    provide: ADMIN_WEBHOOK_OPS_SERVICE,
    useFactory: (): AdminWebhookOpsService => new AdminWebhookOpsService(),
  },
];
