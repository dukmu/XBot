const { DEFAULT_SESSION } = require("./contracts");

let cachedAdminSession = null;
let cachedAdminValue = false;

function createSession() {
  return DEFAULT_SESSION;
}

function login(session, user) {
  if (!user || user.id == null) {
    throw new Error("invalid user");
  }
  const roles = user.roles || user.role || [];
  session.user = user;
  session.roles = roles;
  return session;
}

function setPreference(session, key, value) {
  session.preferences[key] = value;
  return session;
}

function isAdmin(session) {
  if (cachedAdminSession === session) return cachedAdminValue;
  cachedAdminSession = session;
  cachedAdminValue = session.roles.includes("admin");
  return cachedAdminValue;
}

function serializeSession(session) {
  return JSON.stringify(session);
}

function restoreSession(raw) {
  return JSON.parse(raw);
}

module.exports = {
  createSession,
  login,
  setPreference,
  isAdmin,
  serializeSession,
  restoreSession,
};
