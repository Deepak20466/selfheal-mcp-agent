const express = require("express");
const { trace } = require("@opentelemetry/api");
const { displayName } = require("./users");

function displayNameHandler(req, res) {
  res.json({ name: displayName(Number(req.params.id)) });
}

function errorHandler(err, _req, res, _next) {
  const span = trace.getTracer("node_app").startSpan("unhandled-error");
  span.recordException(err);
  span.end();
  res.status(500).json({ error: err.message });
}

function createApp() {
  const app = express();

  app.get("/healthz", (_req, res) => res.json({ status: "ok" }));

  app.get("/users/:id/display-name", displayNameHandler);

  // Error middleware: record the exception (with its stack) on a span so the
  // OTel SDK exports it as an `exception` span event.
  app.use(errorHandler);

  return app;
}

module.exports = { createApp, displayNameHandler, errorHandler };
