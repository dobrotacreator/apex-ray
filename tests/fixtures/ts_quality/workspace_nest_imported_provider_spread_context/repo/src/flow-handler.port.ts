export interface FlowHandler {
  supports(route: string): boolean;
}
