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
class OperatorAuthGuard {}
class PermissionGuard {}
export enum Permission {
  WEBHOOK_INBOX_VIEW = 'WEBHOOK_INBOX_VIEW',
  WEBHOOK_RETRIGGER = 'WEBHOOK_RETRIGGER',
}
@Controller('admin/webhooks/inbox')
@UseGuards(OperatorAuthGuard, PermissionGuard)
export class AdminWebhookController {
  @Post(':id/retrigger')
  @RequirePermission(Permission.WEBHOOK_RETRIGGER)
  retrigger(id: string, actorId: string): string {
    return id;
  }
}
