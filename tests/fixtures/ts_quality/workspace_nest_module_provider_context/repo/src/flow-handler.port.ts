export interface FlowHandler {
  /** Must return true only for routes this handler exclusively owns. */
  supports(route: string): boolean;
}
