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

export class PermissionGuard {
  private readonly reflector = new Reflector();

  canActivate(context: ExecutionContext): boolean {
    const requiredPermission = this.reflector.getAllAndOverride<Permission>(REQUIRED_PERMISSION_KEY, [
      context.getHandler(),
      context.getClass(),
    ]);
    return Boolean(requiredPermission);
  }
}
