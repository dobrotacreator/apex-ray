import type { SettingsStore } from "@acme/settings";

export async function buildDynamicSettingsCachePreview(store: SettingsStore, userId: string) {
  const { tenantScopedSettingsKey } = await import("@acme/settings");
  const alphaKey = tenantScopedSettingsKey("tenant-alpha", userId);
  const betaKey = tenantScopedSettingsKey("tenant-beta", userId);

  return {
    alphaKey,
    betaKey,
    sameKey: alphaKey === betaKey,
    store,
  };
}
