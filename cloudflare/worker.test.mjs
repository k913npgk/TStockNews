import assert from "node:assert/strict";
import { test } from "node:test";
import worker, { taiwanDate, tick } from "./worker.mjs";

const env = { GITHUB_REPOSITORY: "example/stocks", GITHUB_TOKEN: "test-secret", DISPATCH_ENABLED: "true" };
const event = cron => ({ cron, scheduledTime: Date.parse("2026-10-05T13:07:00Z") });
const file = value => new Response(JSON.stringify({ encoding: "base64", content: Buffer.from(JSON.stringify(value)).toString("base64") }));

function api({ delivery = null, status = null, runs = [], dispatchCode = 200 } = {}) {
  const calls = [];
  const fetchFn = async (url, options) => {
    calls.push({ url, options });
    if (url.includes("contents/data/delivery/")) return delivery ? file(delivery) : new Response(null, { status: 404 });
    if (url.includes("contents/data/status.json")) return status ? file(status) : new Response(null, { status: 404 });
    if (url.includes("/runs?")) return new Response(JSON.stringify({ workflow_runs: runs }));
    if (url.endsWith("/dispatches")) return dispatchCode === 204 ? new Response(null, { status: 204 }) : new Response(JSON.stringify({ workflow_run_id: 123 }));
    assert.fail(`Unexpected API path: ${url}`);
  };
  return { fetchFn, calls, log: () => {} };
}

test("Taiwan date uses scheduledTime, including a delayed event across midnight", () => {
  assert.equal(taiwanDate(Date.parse("2026-12-31T16:01:00Z")), "2027-01-01");
  assert.equal(taiwanDate(event().scheduledTime), "2026-10-05");
  assert.throws(() => taiwanDate(NaN));
});

test("disabled scheduler performs no requests", async () => {
  const fake = api();
  assert.equal((await tick(event("7 13 * * *"), { ...env, DISPATCH_ENABLED: "false" }, fake)).status, "DISABLED");
  assert.equal(fake.calls.length, 0);
});

test("SENT suppresses all dispatch/check phases and only reads production state", async () => {
  for (const cron of ["7 13 * * *", "20 13 * * *", "30 13 * * *"]) {
    const fake = api({ delivery: { status: "SENT" } });
    assert.equal((await tick(event(cron), env, fake)).status, "SENT");
    assert.equal(fake.calls.length, 1);
    assert.ok(fake.calls[0].url.endsWith("?ref=screener-data"));
    assert.equal(fake.calls[0].options.method, "GET");
  }
});

test("only same-date non-trading-day state suppresses dispatch", async () => {
  const closed = api({ status: { date: "2026-10-05", status: "NON_TRADING_DAY" } });
  assert.equal((await tick(event("30 13 * * *"), env, closed)).status, "NON_TRADING_DAY");
  const stale = api({ status: { date: "2026-10-04", status: "NON_TRADING_DAY" } });
  assert.equal((await tick(event("7 13 * * *"), env, stale)).status, "DISPATCHED");
});

test("primary and missing-run fallback dispatch the scheduled date to main", async () => {
  for (const cron of ["7 13 * * *", "20 13 * * *"]) {
    for (const dispatchCode of [200, 204]) {
      const fake = api({ dispatchCode });
      const result = await tick(event(cron), env, fake);
      assert.equal(result.status, "DISPATCHED");
      const posts = fake.calls.filter(call => call.options.method === "POST");
      assert.equal(posts.length, 1);
      assert.deepEqual(JSON.parse(posts[0].options.body), {
        ref: "main", inputs: { mode: "daily", date: "2026-10-05", send_line: true, scheduler: "cloudflare" },
      });
      assert.ok(fake.calls[2].url.includes("branch=main"));
      const window = new URL(fake.calls[2].url).searchParams.get("created");
      assert.equal(window, ">=2026-10-04T16:00:00Z");
    }
  }
});

test("queued, running, and legacy active workflows never get a duplicate dispatch", async () => {
  for (const status of ["queued", "in_progress", "waiting", "pending", "requested"]) {
    for (const title of ["stock-screener daily 2026-10-05", "Daily Taiwan stock screener", "stock-screener daily scheduled"]) {
      const fake = api({ runs: [{ id: 7, status, display_title: title }] });
      assert.equal((await tick(event("20 13 * * *"), env, fake)).status, "RUN_ACTIVE");
      assert.ok(fake.calls.every(call => call.options.method === "GET"));
    }
  }
});

test("completed unsent/failed/cancelled daily run requires investigation", async () => {
  for (const conclusion of ["success", "failure", "cancelled"]) {
    const fake = api({ runs: [{ id: 7, status: "completed", conclusion, display_title: "stock-screener daily 2026-10-05" }] });
    assert.equal((await tick(event("20 13 * * *"), env, fake)).status, "RUN_EXISTS");
    assert.ok(fake.calls.every(call => call.options.method === "GET"));
  }
});

test("an isolated test or yesterday's completed run cannot satisfy today's trigger", async () => {
  const fake = api({ runs: [
    { status: "completed", display_title: "stock-screener daily 2026-10-04" },
    { status: "in_progress", display_title: "stock-screener test 2026-10-05" },
  ] });
  assert.equal((await tick(event("20 13 * * *"), env, fake)).status, "DISPATCHED");
});

test("21:30 reports missing delivery even for successful or active workflows; never dispatches", async () => {
  for (const status of ["completed", "in_progress"]) {
    const fake = api({ runs: [{ id: 7, status, conclusion: "success", display_title: "stock-screener daily 2026-10-05" }] });
    await assert.rejects(tick(event("30 13 * * *"), env, fake), /Delivery not confirmed/);
    assert.ok(fake.calls.every(call => call.options.method === "GET"));
  }
});

test("API/auth/schema failures stop before dispatch and do not leak response bodies", async () => {
  for (const code of [301, 307, 401, 403, 429, 500]) {
    await assert.rejects(tick(event("7 13 * * *"), env, {
      fetchFn: async () => new Response("sensitive-response", { status: code }), log: () => {},
    }), error => !error.message.includes("sensitive-response") && error.message.includes(String(code)));
  }
  await assert.rejects(tick(event("7 13 * * *"), env, { fetchFn: async () => file({}), log: () => {} }), /Unexpected workflow/);
});

test("pagination does not falsely conclude no run exists", async () => {
  const fake = api({ runs: Array.from({ length: 100 }, () => ({ status: "completed", display_title: "stock-screener test 2026-10-05" })) });
  await assert.rejects(tick(event("20 13 * * *"), env, fake), /pagination limit/);
  assert.equal(fake.calls.filter(call => call.url.includes("/runs?")).length, 5);
  assert.ok(fake.calls.every(call => call.options.method === "GET"));
});

test("production state cannot use a test-only SENT receipt", async () => {
  const fake = api({ delivery: { status: "SENT", test_only: true } });
  await assert.rejects(tick(event("7 13 * * *"), env, fake), /Test delivery/);
  assert.equal(fake.calls.length, 1);
});

test("Cron handler awaits failures; worker has no public HTTP entry point", async () => {
  assert.equal(worker.fetch, undefined);
  await assert.rejects(worker.scheduled(event("unexpected"), env), /Unexpected cron/);
});
