export enum Permission {
  WEBHOOK_INBOX_VIEW = 'WEBHOOK_INBOX_VIEW',
}

export const ROLE_PERMISSIONS = {
  ADMIN: [Permission.WEBHOOK_INBOX_VIEW],
} as const;
