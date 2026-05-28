export class InvalidStateTransitionError extends Error {}

export function createStateMachine<S extends string, E extends string>(
  _name: string,
  transitions: ReadonlyArray<readonly [S, E, S]>,
): (from: S, event: E) => { ok: true; value: S } | { ok: false; error: InvalidStateTransitionError } {
  return (from, event) => {
    const match = transitions.find(
      ([candidateFrom, candidateEvent]) => candidateFrom === from && candidateEvent === event,
    );
    return match
      ? { ok: true, value: match[2] }
      : { ok: false, error: new InvalidStateTransitionError(`${from}:${event}`) };
  };
}
