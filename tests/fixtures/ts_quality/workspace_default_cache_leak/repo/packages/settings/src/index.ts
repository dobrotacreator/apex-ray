export interface Settings {
  theme: string;
  emailEnabled: boolean;
}

export interface SettingsStore {
  fetchSettings(tenantId: string, userId: string): Promise<Settings>;
}

const settingsCache = new Map<string, Promise<Settings>>();

export default function tenantScopedSettingsKey(tenantId: string, userId: string): string {
  return userId;
}

export async function loadSharedSettings(
  store: SettingsStore,
  tenantId: string,
  userId: string,
): Promise<Settings> {
  const key = tenantScopedSettingsKey(tenantId, userId);
  let cached = settingsCache.get(key);
  if (!cached) {
    cached = store.fetchSettings(tenantId, userId);
    settingsCache.set(key, cached);
  }

  return cached;
}
