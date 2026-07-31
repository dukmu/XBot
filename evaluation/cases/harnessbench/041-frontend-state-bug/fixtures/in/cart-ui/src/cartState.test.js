const assert = require("assert");
const { createCart, addItem, updateQuantity, removeItem, getSubtotal, undo, redo, serializeCart, restoreCart, createSubtotalSelector } = require("./cartState");

let cart = createCart();
let next = addItem(cart, { sku: "tea", name: "Tea", unitCents: 500, quantity: 2 });
assert.notStrictEqual(next, cart, "addItem must return a new cart object");
assert.deepStrictEqual(cart.items, [], "original cart must not be mutated");
assert.strictEqual(getSubtotal(next), 1000);

let updated = updateQuantity(next, "tea", 3);
assert.notStrictEqual(updated, next, "updateQuantity must be immutable");
assert.strictEqual(getSubtotal(next), 1000, "previous cart subtotal must stay stable");
assert.strictEqual(getSubtotal(updated), 1500);

let removedByZero = updateQuantity(updated, "tea", 0);
assert.deepStrictEqual(removedByZero.items, [], "zero quantity removes the line");

let withCoffee = addItem(updated, { sku: "coffee", name: "Coffee", unitCents: 800, quantity: 1 });
let removed = removeItem(withCoffee, "tea");
assert.deepStrictEqual(removed.items.map((item) => item.sku), ["coffee"]);
assert.strictEqual(getSubtotal(removed), 800);

assert.throws(() => addItem(updated, { sku: "bad", name: "Bad", unitCents: 100, quantity: -1 }), /quantity/i);
assert.throws(() => updateQuantity(updated, "tea", -2), /quantity/i);

let selector = createSubtotalSelector();
assert.strictEqual(selector(updated), 1500);
let updatedAgain = updateQuantity(updated, "tea", 4);
assert.strictEqual(selector(updatedAgain), 2000, "selector must not reuse stale subtotal after immutable update");

let undone = undo(updatedAgain);
assert.strictEqual(getSubtotal(undone), 1500, "undo restores previous cart snapshot");
let redone = redo(undone);
assert.strictEqual(getSubtotal(redone), 2000, "redo restores undone cart snapshot");

let persisted = serializeCart(redone);
let restored = restoreCart(persisted);
assert.deepStrictEqual(restored.items, redone.items, "restoreCart keeps persisted items");
assert.notStrictEqual(restored.items, redone.items, "restoreCart must not share item arrays");

let cartA = addItem(createCart(), { sku: "a", name: "A", unitCents: 100, quantity: 1 });
let cartB = addItem(createCart(), { sku: "b", name: "B", unitCents: 200, quantity: 1 });
assert.deepStrictEqual(cartA.items.map((item) => item.sku), ["a"], "carts must not share state");
assert.deepStrictEqual(cartB.items.map((item) => item.sku), ["b"], "carts must not share state");

console.log("cartState tests passed");
