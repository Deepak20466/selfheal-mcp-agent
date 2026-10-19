const test = require("node:test");
const assert = require("node:assert");
const { displayNameHandler, errorHandler } = require("../src/app");

function callDisplayNameRoute(id) {
  let status = 200;
  let body;
  const response = {
    json(value) {
      body = value;
      return this;
    },
    status(value) {
      status = value;
      return this;
    },
  };

  try {
    displayNameHandler({ params: { id } }, response);
  } catch (error) {
    errorHandler(error, {}, response, () => {});
  }

  return { status, body };
}

test("known user gets an upper-cased display name", () => {
  assert.deepStrictEqual(callDisplayNameRoute("1"), {
    status: 200,
    body: { name: "ADA" },
  });
});

test("seeded Sentinel demo failure: unknown user currently returns HTTP 500", () => {
  // This fixture intentionally preserves the TypeError that Sentinel is
  // meant to detect. The request/error handlers run in-process because the
  // OS sandbox denies loopback network access to repository tests.
  const result = callDisplayNameRoute("999");
  assert.strictEqual(result.status, 500);
  assert.strictEqual(typeof result.body.error, "string");
  assert.match(result.body.error, /undefined/);
});
