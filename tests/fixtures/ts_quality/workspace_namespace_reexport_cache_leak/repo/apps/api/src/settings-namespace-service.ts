import * as settings from "@acme/settings";

export async function buildNamespaceSettingsCachePreview(userId: string) {
  const alphaKey = settings.settingsKeys.buildTenantSettingsKey("tenant-alpha", userId);
  const betaKey = settings.settingsKeys.buildTenantSettingsKey("tenant-beta", userId);

  return {
    alphaKey,
    betaKey,
    sameKey: alphaKey === betaKey,
  };
}
