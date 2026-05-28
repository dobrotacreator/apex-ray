import { publicTenantSettingsKey } from "../../../packages/settings/src";

export async function buildRelativeSettingsCachePreview(userId: string) {
  const alphaKey = publicTenantSettingsKey("tenant-alpha", userId);
  const betaKey = publicTenantSettingsKey("tenant-beta", userId);

  return {
    alphaKey,
    betaKey,
    sameKey: alphaKey === betaKey,
  };
}
