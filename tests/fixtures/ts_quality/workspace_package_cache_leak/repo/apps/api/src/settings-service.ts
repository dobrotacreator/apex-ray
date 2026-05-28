import { SettingsStore, tenantScopedSettingsKey } from "@acme/settings";

export async function buildSettingsCachePreview(store: SettingsStore, userId: string) {
  const alphaKey = tenantScopedSettingsKey("tenant-alpha", userId);
  const betaKey = tenantScopedSettingsKey("tenant-beta", userId);

  return {
    alphaKey,
    betaKey,
    sameKey: alphaKey === betaKey,
    store,
  };
}
