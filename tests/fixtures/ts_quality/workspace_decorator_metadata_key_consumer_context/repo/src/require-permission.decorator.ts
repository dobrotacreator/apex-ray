import { Permission } from './permissions.js';

export const REQUIRED_PERMISSION_KEY = 'required_permission';

function SetMetadata(_key: string, _value: unknown): MethodDecorator {
  return () => undefined;
}

export function RequirePermission(permission: Permission): MethodDecorator {
  return SetMetadata(REQUIRED_PERMISSION_KEY, permission);
}
