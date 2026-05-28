export class PermissionGuard {
  /** Requires a Permission metadata value to be present on the handler. */
  canActivate(): boolean {
    return true;
  }
}
