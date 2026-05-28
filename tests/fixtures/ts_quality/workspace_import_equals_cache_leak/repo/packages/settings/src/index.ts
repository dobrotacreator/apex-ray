export interface Settings {
  theme: string;
  emailEnabled: boolean;
}

export interface SettingsStore {
  fetchSettings(tenantId: string, userId: string): Promise<Settings>;
}

const settingsCache = new Map<string, Promise<Settings>>();

function tenantScopedSettingsKey(tenantId: string, userId: string): string {
  return userId;
}

export = tenantScopedSettingsKey;
