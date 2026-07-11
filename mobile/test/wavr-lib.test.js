const { test } = require("node:test");
const assert = require("node:assert");
const { isHost, isPort, parseCoreService, consentToActions } = require("../src/wavr-lib.js");

test("isPort accepts 1..65535, rejects others", () => {
  assert.equal(isPort("8000"), true);
  assert.equal(isPort("1"), true);
  assert.equal(isPort("65535"), true);
  assert.equal(isPort("0"), false);
  assert.equal(isPort("70000"), false);
  assert.equal(isPort("abc"), false);
});

test("isHost accepts IPv4 and hostnames, rejects junk", () => {
  assert.equal(isHost("192.168.1.50"), true);
  assert.equal(isHost("wavr-core.local"), true);
  assert.equal(isHost("bad host"), false);
  assert.equal(isHost(""), false);
});

test("parseCoreService keeps role=core with valid host/port", () => {
  const r = { name: "Wavr Core", hostname: "192.168.1.50", port: 8000, txtRecord: { v: "1", role: "core" } };
  assert.deepEqual(parseCoreService(r), { name: "Wavr Core", host: "192.168.1.50", port: 8000 });
});

test("parseCoreService prefers the routable ipv4 over the unresolvable .local hostname", () => {
  // Real capacitor-zeroconf "resolved" shape: hostname is the mDNS SRV target (.local., not
  // resolvable by native TLS), ipv4Addresses carries the concrete address to actually connect to.
  const r = { name: "Wavr Core", hostname: "android-abcd.local.", ipv4Addresses: ["192.168.1.57"],
              port: 8000, txtRecord: { v: "1", role: "core", path: "/?core" } };
  assert.deepEqual(parseCoreService(r), { name: "Wavr Core", host: "192.168.1.57", port: 8000 });
});

test("parseCoreService rejects non-core / invalid", () => {
  assert.equal(parseCoreService({ name: "x", hostname: "192.168.1.50", port: 8000, txtRecord: { role: "printer" } }), null);
  assert.equal(parseCoreService({ name: "x", hostname: "bad host", port: 8000, txtRecord: { role: "core" } }), null);
  assert.equal(parseCoreService({ name: "x", hostname: "192.168.1.50", port: 0, txtRecord: { role: "core" } }), null);
  assert.equal(parseCoreService(null), null);
});

test("consentToActions maps the three levels", () => {
  assert.deepEqual(consentToActions("green"),  { attached: true,  presence: "register", contribution: "full" });
  assert.deepEqual(consentToActions("yellow"), { attached: true,  presence: "register", contribution: "limited" });
  assert.deepEqual(consentToActions("red"),    { attached: false, presence: "delete",   contribution: "none" });
});
