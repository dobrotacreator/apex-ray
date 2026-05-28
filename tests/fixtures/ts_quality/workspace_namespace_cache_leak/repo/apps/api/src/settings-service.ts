import * as settings from "@acme/settings";

export async function buildSettingsCachePreview(userId: string) {
  const alphaKey = settings.tenantScopedSettingsKey("tenant-alpha", userId);
  const betaKey = settings.tenantScopedSettingsKey("tenant-beta", userId);

  return {
    alphaKey,
    betaKey,
    sameKey: alphaKey === betaKey,
  };
}
