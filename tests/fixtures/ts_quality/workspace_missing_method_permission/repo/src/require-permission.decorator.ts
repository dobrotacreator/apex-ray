import { Permission } from './permissions.js';

export const REQUIRED_PERMISSION_KEY = 'required_permission';

function SetMetadata(_key: string, _value: unknown): MethodDecorator & ClassDecorator {
  return () => undefined;
}

export function RequirePermission(...permissions: Permission[]): MethodDecorator & ClassDecorator {
  return SetMetadata(REQUIRED_PERMISSION_KEY, permissions);
}
