export interface Settings {
  theme: string;
  emailEnabled: boolean;
}

export interface SettingsStore {
  fetchSettings(tenantId: string, userId: string): Promise<Settings>;
}

const settingsCache = new Map<string, Promise<Settings>>();

export function settingsCacheKey(tenantId: string, userId: string): string {
  return userId;
}

export async function loadUserSettings(
  store: SettingsStore,
  tenantId: string,
  userId: string,
): Promise<Settings> {
  const key = settingsCacheKey(tenantId, userId);
  let cached = settingsCache.get(key);
  if (!cached) {
    cached = store.fetchSettings(tenantId, userId);
    settingsCache.set(key, cached);
  }

  return cached;
}
