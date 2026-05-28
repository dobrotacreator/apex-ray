export const Permission = {
  FINANCE_REPORT_VIEW: 'FINANCE_REPORT_VIEW',
  FINANCE_REPORT_EXPORT: 'FINANCE_REPORT_EXPORT',
} as const;

export type Permission = (typeof Permission)[keyof typeof Permission];
