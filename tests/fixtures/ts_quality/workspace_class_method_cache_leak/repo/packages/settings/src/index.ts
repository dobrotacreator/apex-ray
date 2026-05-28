export interface Settings {
  theme: string;
  emailEnabled: boolean;
}

export interface SettingsStore {
  fetchSettings(tenantId: string, userId: string): Promise<Settings>;
}

const settingsCache = new Map<string, Promise<Settings>>();

export class SettingsClient {
  constructor(private readonly store: SettingsStore) {}

  buildTenantSettingsKey(tenantId: string, userId: string): string {
    return userId;
  }

  async loadSharedSettings(tenantId: string, userId: string): Promise<Settings> {
    const key = this.buildTenantSettingsKey(tenantId, userId);
    let cached = settingsCache.get(key);
    if (!cached) {
      cached = this.store.fetchSettings(tenantId, userId);
      settingsCache.set(key, cached);
    }

    return cached;
  }
}
