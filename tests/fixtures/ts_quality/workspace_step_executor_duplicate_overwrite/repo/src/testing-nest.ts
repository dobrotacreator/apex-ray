export function Inject(_token: unknown): ParameterDecorator {
  return () => undefined;
}

export function Injectable(): ClassDecorator {
  return () => undefined;
}

export function Module(_metadata: unknown): ClassDecorator {
  return () => undefined;
}
