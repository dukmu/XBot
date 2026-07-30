const assert = require("assert");
const store = require("./sessionStore");

const a = store.createSession();
const b = store.createSession();
assert.notStrictEqual(a, b, "createSession must return fresh objects");

const user = { id: 42, name: "Mira", roles: "admin", profile: { team: "ops" } };
const logged = store.login(a, user);
assert.strictEqual(a.user, null, "login must not mutate input session");
assert.strictEqual(logged.user.id, "42");
assert.deepStrictEqual(logged.roles, ["admin"]);
assert.strictEqual(store.isAdmin(logged), true);

user.profile.team = "sales";
assert.strictEqual(logged.user.profile.team, "ops", "session must not retain user object by reference");

const noAdmin = store.login(logged, { id: "43", name: "Noor", roles: ["viewer"] });
assert.strictEqual(store.isAdmin(noAdmin), false, "admin selector must not be stale after login");

const pref = store.setPreference(noAdmin, "theme", { mode: "dark" });
assert.deepStrictEqual(noAdmin.preferences, {}, "setPreference must not mutate input session");
assert.deepStrictEqual(pref.preferences.theme, { mode: "dark" });

const restored = store.restoreSession(store.serializeSession(pref));
restored.preferences.theme.mode = "light";
assert.strictEqual(pref.preferences.theme.mode, "dark", "restore must not share nested objects");

assert.throws(() => store.login(pref, { id: "", roles: [] }), /invalid|id/i);
assert.throws(() => store.login(pref, { id: "x", roles: [3] }), /role/i);
assert.throws(() => store.setPreference(pref, "", true), /key/i);
console.log("session store tests passed");
