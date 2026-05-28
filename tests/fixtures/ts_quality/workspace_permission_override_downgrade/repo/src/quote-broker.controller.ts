import { Permission } from './permissions.js';
import { RequirePermission } from './require-permission.decorator.js';
import { PermissionGuard } from './permission.guard.js';

function Controller(_path: string): ClassDecorator {
  return () => undefined;
}

function UseGuards(..._guards: unknown[]): ClassDecorator {
  return () => undefined;
}

function Get(_path?: string): MethodDecorator {
  return () => undefined;
}

function Post(_path: string): MethodDecorator {
  return () => undefined;
}

class QuoteBrokerService {
  list(): string[] {
    return [];
  }

  cancel(quoteId: string): string {
    return quoteId;
  }
}

@Controller('broker/quotes')
@UseGuards(PermissionGuard)
@RequirePermission(Permission.QUOTE_BROKER_VIEW)
export class QuoteBrokerController {
  private readonly service = new QuoteBrokerService();

  @Get()
  @RequirePermission(Permission.QUOTE_BROKER_VIEW)
  list(): string[] {
    return this.service.list();
  }

  @Post(':quoteId/cancel')
  @RequirePermission(Permission.QUOTE_BROKER_VIEW)
  cancel(quoteId: string): string {
    return this.service.cancel(quoteId);
  }
}
