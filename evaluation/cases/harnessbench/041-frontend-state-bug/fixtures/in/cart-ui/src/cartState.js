function createCart() {
  return { items: [] };
}

function addItem(cart, item) {
  const existing = cart.items.find((entry) => entry.sku === item.sku);
  if (existing) {
    existing.quantity += item.quantity;
    return cart;
  }
  cart.items.push({ ...item });
  return cart;
}

function updateQuantity(cart, sku, quantity) {
  const item = cart.items.find((entry) => entry.sku === sku);
  if (!item) {
    return cart;
  }
  item.quantity = quantity;
  return cart;
}

function removeItem(cart, sku) {
  cart.items = cart.items.filter((entry) => entry.sku !== sku);
  return cart;
}

function getSubtotal(cart) {
  return cart.items.reduce((sum, item) => sum + item.unitCents * item.quantity, 0);
}

module.exports = { createCart, addItem, updateQuantity, removeItem, getSubtotal };
