import { Permission } from './permissions.js';
import { RequirePermission } from './require-permission.decorator.js';
function Controller(_path: string): ClassDecorator {
  return () => undefined;
}
function Post(_path: string): MethodDecorator {
  return () => undefined;
}
@Controller('admin/webhooks/inbox')
export class AdminWebhookController {
  @Post(':id/retrigger')
  @RequirePermission(Permission.WEBHOOK_RETRIGGER)
  retrigger(id: string, actorId: string): string {
    return id;
  }
}
