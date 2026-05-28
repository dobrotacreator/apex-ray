import { workspaceCacheKey } from "@settings/cache";

export async function buildSettingsCachePreview(userId: string) {
  const alphaKey = workspaceCacheKey("tenant-alpha", userId);
  const betaKey = workspaceCacheKey("tenant-beta", userId);

  return {
    alphaKey,
    betaKey,
    sameKey: alphaKey === betaKey,
  };
}
