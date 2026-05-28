import { calculateTotal, CartItem } from "./cart";

export function checkout(items: CartItem[]) {
  return {
    total: calculateTotal(items),
  };
}
