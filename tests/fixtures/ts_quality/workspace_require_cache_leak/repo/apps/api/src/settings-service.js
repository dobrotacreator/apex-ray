const { tenantScopedSettingsKey } = require("@acme/settings");

exports.buildSettingsCachePreview = function buildSettingsCachePreview(store, userId) {
  const alphaKey = tenantScopedSettingsKey("tenant-alpha", userId);
  const betaKey = tenantScopedSettingsKey("tenant-beta", userId);

  return {
    alphaKey,
    betaKey,
    sameKey: alphaKey === betaKey,
    store,
  };
};
