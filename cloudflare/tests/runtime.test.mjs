import assert from "node:assert/strict";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { createTestHarness } from "wrangler";

for (const scenario of ["sent", "redirect"]) {
  test(`Workers native fetch: ${scenario}`, async () => {
    const server = createTestHarness({
      workers: [
        { config: {
          name: "scheduler-test", main: fileURLToPath(new URL("runtime-worker.mjs", import.meta.url)),
          compatibility_date: "2026-10-05",
          vars: { DISPATCH_ENABLED: "true", GITHUB_REPOSITORY: "example/stocks", GITHUB_TOKEN: "runtime-test-only" },
          services: [{ binding: "GITHUB_API", service: "github-stub" }],
        } },
        { config: {
          name: "github-stub", main: fileURLToPath(new URL("github-stub.mjs", import.meta.url)),
          compatibility_date: "2026-10-05", vars: { CASE: scenario },
        } },
      ],
    });
    try {
      await server.listen();
      const result = await server.getWorker("scheduler-test").scheduled({
        cron: "7 13 * * *", scheduledTime: new Date("2026-10-05T13:07:00Z"),
      });
      assert.equal(result.outcome, scenario === "sent" ? "ok" : "exception");
    } finally {
      await server.close();
    }
  });
}
