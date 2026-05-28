export type HandlerVersion = string & { readonly __brand: 'HandlerVersion' };

const SEMVER_RE = /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/;

export function handlerVersion(value: string): HandlerVersion {
  if (!SEMVER_RE.test(value)) {
    throw new Error(`Invalid handler version: ${value}`);
  }
  return value as HandlerVersion;
}

export function compareHandlerVersions(a: HandlerVersion, b: HandlerVersion): number {
  return a.localeCompare(b);
}
