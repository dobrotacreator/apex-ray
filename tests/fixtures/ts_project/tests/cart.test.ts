import { calculateTotal } from "../src/cart";

it("calculates totals", () => {
  expect(calculateTotal([{ price: 10, quantity: 2 }])).toBe(20);
});
