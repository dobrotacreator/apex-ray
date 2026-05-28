import { publicTenantSettingsKey } from "@acme/settings";

export async function buildSettingsCachePreview(userId: string) {
  const alphaKey = publicTenantSettingsKey("tenant-alpha", userId);
  const betaKey = publicTenantSettingsKey("tenant-beta", userId);

  return {
    alphaKey,
    betaKey,
    sameKey: alphaKey === betaKey,
  };
}
