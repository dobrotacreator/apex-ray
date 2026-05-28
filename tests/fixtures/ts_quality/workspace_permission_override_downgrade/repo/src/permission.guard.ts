import { Permission } from './permissions.js';
import { REQUIRED_PERMISSION_KEY } from './require-permission.decorator.js';

interface ExecutionContext {
  getHandler(): unknown;
  getClass(): unknown;
}

class Reflector {
  getAllAndOverride<T>(_key: string, _targets: unknown[]): T | undefined {
    return undefined;
  }
}

interface Request {
  operator: { permissions: Set<Permission> };
}

export class PermissionGuard {
  private readonly reflector = new Reflector();

  canActivate(context: ExecutionContext, request: Request): boolean {
    const requiredPermissions = this.reflector.getAllAndOverride<Permission[]>(REQUIRED_PERMISSION_KEY, [
      context.getHandler(),
      context.getClass(),
    ]);
    if (!requiredPermissions) return true;
    return requiredPermissions.every((permission) => request.operator.permissions.has(permission));
  }
}
