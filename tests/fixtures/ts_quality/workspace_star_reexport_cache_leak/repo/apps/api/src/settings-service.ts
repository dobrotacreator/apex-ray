import { buildTenantSettingsKey } from "../../../packages/settings/src";

export async function buildSettingsCachePreview(userId: string) {
  const alphaKey = buildTenantSettingsKey("tenant-alpha", userId);
  const betaKey = buildTenantSettingsKey("tenant-beta", userId);

  return {
    alphaKey,
    betaKey,
    sameKey: alphaKey === betaKey,
  };
}
