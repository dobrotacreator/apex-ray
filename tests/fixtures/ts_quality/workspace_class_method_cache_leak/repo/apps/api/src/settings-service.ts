import { SettingsClient } from "@acme/settings";

export function buildSettingsCachePreview(client: SettingsClient, userId: string) {
  const alphaKey = client.buildTenantSettingsKey("tenant-alpha", userId);
  const betaKey = client.buildTenantSettingsKey("tenant-beta", userId);

  return {
    alphaKey,
    betaKey,
    sameKey: alphaKey === betaKey,
  };
}
