import { PermissionGuard } from './permission.guard.js';
import { Permission } from './permissions.js';
import { RequirePermission } from './require-permission.decorator.js';

function Controller(_path: string): ClassDecorator {
  return () => undefined;
}

function UseGuards(..._guards: unknown[]): ClassDecorator {
  return () => undefined;
}

function Get(): MethodDecorator {
  return () => undefined;
}

function Post(_path: string): MethodDecorator {
  return () => undefined;
}

@Controller('admin/quote/reconcile')
@UseGuards(PermissionGuard)
export class AdminQuoteReconcileController {
  @Get()
  @RequirePermission(Permission.QUOTE_RECONCILE_MANAGE)
  listCandidates(): string {
    return 'candidates';
  }

  @Post(':id/retry')
  retryReconcile(): string {
    return 'retry';
  }
}
