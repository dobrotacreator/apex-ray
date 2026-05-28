export async function buildDynamicNamespaceSettingsCachePreview(userId: string) {
  const settings = await import("@acme/settings");
  const alphaKey = settings.tenantScopedSettingsKey("tenant-alpha", userId);
  const betaKey = settings.tenantScopedSettingsKey("tenant-beta", userId);

  return {
    alphaKey,
    betaKey,
    sameKey: alphaKey === betaKey,
  };
}
