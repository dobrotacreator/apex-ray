import tenantSettingsKey from "@acme/settings";

export async function buildSettingsCachePreview(userId: string) {
  const alphaKey = tenantSettingsKey("tenant-alpha", userId);
  const betaKey = tenantSettingsKey("tenant-beta", userId);

  return {
    alphaKey,
    betaKey,
    sameKey: alphaKey === betaKey,
  };
}
