import { Permission } from './permissions.js';
import { RequirePermission } from './require-permission.decorator.js';
import { PermissionGuard } from './permission.guard.js';

function Controller(_path: string): ClassDecorator {
  return () => undefined;
}

function UseGuards(..._guards: unknown[]): ClassDecorator {
  return () => undefined;
}

function Get(_path: string): MethodDecorator {
  return () => undefined;
}

@Controller('admin/finance/reports')
@UseGuards(PermissionGuard)
export class AdminFinanceReportsController {
  @Get(':id')
  @RequirePermission(Permission.FINANCE_REPORT_VIEW)
  detail(reportId: string): string {
    return reportId;
  }

  @Get(':id/export')
  exportCsv(reportId: string): string {
    return `csv:${reportId}`;
  }
}
