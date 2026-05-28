import tenantScopedSettingsKey = require("@acme/settings");

export async function buildSettingsCachePreview(userId: string) {
  const alphaKey = tenantScopedSettingsKey("tenant-alpha", userId);
  const betaKey = tenantScopedSettingsKey("tenant-beta", userId);

  return {
    alphaKey,
    betaKey,
    sameKey: alphaKey === betaKey,
  };
}
