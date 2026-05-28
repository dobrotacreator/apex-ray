import { Permission } from './permissions.js';
import { PermissionGuard } from './guards.js';
function Controller(_path: string): ClassDecorator {
  return () => undefined;
}
function UseGuards(..._guards: unknown[]): ClassDecorator {
  return () => undefined;
}
function Post(_path: string): MethodDecorator {
  return () => undefined;
}
function RequirePermission(_permission: Permission): MethodDecorator {
  return () => undefined;
}
@Controller('admin/webhooks/inbox')
@UseGuards(PermissionGuard)
export class AdminWebhookController {
  @Post(':id/retrigger')
  @RequirePermission(Permission.WEBHOOK_RETRIGGER)
  retrigger(id: string, actorId: string): string {
    return id;
  }
}
