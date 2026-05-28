import { settingsKeys } from "@acme/settings";

export async function buildSettingsCachePreview(userId: string) {
  const alphaKey = settingsKeys.buildTenantSettingsKey("tenant-alpha", userId);
  const betaKey = settingsKeys.buildTenantSettingsKey("tenant-beta", userId);

  return {
    alphaKey,
    betaKey,
    sameKey: alphaKey === betaKey,
  };
}
