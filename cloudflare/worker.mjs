// This Worker only dispatches/checks Actions. Official data and LINE stay on the runner.
const CRONS = new Map([
  ["7 13 * * *", "dispatch"],
  ["20 13 * * *", "check-run"],
  ["30 13 * * *", "check-delivery"],
]);
const ACTIVE = new Set(["queued", "in_progress", "waiting", "pending", "requested"]);
const WORKFLOW = "daily.yml";

export function taiwanDate(scheduledTime) {
  if (!Number.isFinite(scheduledTime)) throw new Error("Invalid scheduled time");
  return new Date(scheduledTime + 8 * 60 * 60 * 1000).toISOString().slice(0, 10);
}

function configuration(env) {
  if (!/^[\w.-]+\/[\w.-]+$/.test(env.GITHUB_REPOSITORY || "")) {
    throw new Error("Invalid GITHUB_REPOSITORY");
  }
  if (!env.GITHUB_TOKEN) throw new Error("Missing GITHUB_TOKEN secret");
}

async function github(env, path, { method = "GET", payload, missing = false } = {}, fetchFn) {
  const response = await fetchFn(`https://api.github.com/repos/${env.GITHUB_REPOSITORY}/${path}`, {
    method,
    headers: {
      Authorization: `Bearer ${env.GITHUB_TOKEN}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2026-03-10",
      "User-Agent": "TStockNews-Cloudflare-Cron",
      ...(payload ? { "Content-Type": "application/json" } : {}),
    },
    ...(payload ? { body: JSON.stringify(payload) } : {}),
    signal: AbortSignal.timeout(15000),
    redirect: "error",
  });
  if (response.status === 404 && missing) return null;
  if (!response.ok) {
    // Never log API response bodies, Authorization, or request payloads.
    throw new Error(`GitHub ${method} failed: HTTP ${response.status}`);
  }
  return response.status === 204 ? null : response.json();
}

async function stateFile(env, path, fetchFn) {
  const file = await github(env, `contents/${path}?ref=screener-data`, { missing: true }, fetchFn);
  if (file === null) return null;
  if (file.encoding !== "base64" || typeof file.content !== "string") {
    throw new Error("Unexpected state file encoding");
  }
  return JSON.parse(new TextDecoder().decode(Uint8Array.from(atob(file.content.replace(/\s/g, "")), c => c.charCodeAt(0))));
}

async function completion(env, day, fetchFn) {
  const delivery = await stateFile(env, `data/delivery/${day}.json`, fetchFn);
  if (delivery?.test_only || delivery?.excluded_from_performance) {
    throw new Error("Test delivery found in production state");
  }
  if (delivery?.status === "SENT") return "SENT";
  const status = await stateFile(env, "data/status.json", fetchFn);
  if (status?.date === day && status.status === "NON_TRADING_DAY") return "NON_TRADING_DAY";
  return null;
}

async function runsForDate(env, day, fetchFn) {
  // The filter starts at the Taiwan day. run-name binds an explicit target date,
  // so an isolated test or yesterday's delayed run cannot satisfy today's check.
  const start = new Date(`${day}T00:00:00+08:00`);
  // Do not cap at midnight: a delayed Cron event can create its run the next day.
  const created = encodeURIComponent(`>=${start.toISOString().replace(".000Z", "Z")}`);
  const matching = [];
  for (let page = 1; page <= 5; page++) {
    const result = await github(env, `actions/workflows/${WORKFLOW}/runs?branch=main&created=${created}&per_page=100&page=${page}`, {}, fetchFn);
    if (!Array.isArray(result.workflow_runs)) throw new Error("Unexpected workflow runs response");
    for (const run of result.workflow_runs) {
      if (run.display_title === `stock-screener daily ${day}`) matching.push(run);
      // A legacy/no-date manual or scheduled run may hold the global state lock.
      // Be conservative while it runs, but do not count it as today's completion.
      else if (ACTIVE.has(run.status) && !/^stock-screener test /.test(run.display_title || "")) {
        matching.push({ ...run, other_active: true });
      }
    }
    if (result.workflow_runs.length < 100) return matching;
  }
  throw new Error("Workflow run pagination limit reached; refused duplicate dispatch");
}

export async function tick(event, env, { fetchFn = fetch, log = value => console.log(JSON.stringify(value)) } = {}) {
  const phase = CRONS.get(event.cron);
  if (!phase) throw new Error("Unexpected cron expression");
  const day = taiwanDate(event.scheduledTime);
  if (env.DISPATCH_ENABLED !== "true") {
    const result = { date: day, phase, status: "DISABLED" };
    log(result);
    return result;
  }
  configuration(env);
  const done = await completion(env, day, fetchFn);
  if (done) {
    const result = { date: day, phase, status: done };
    log(result);
    return result;
  }
  const runs = await runsForDate(env, day, fetchFn);
  const active = runs.find(run => ACTIVE.has(run.status));
  if (phase === "check-delivery") {
    // A successful Actions run alone does not prove delivery. Fail the Cron check
    // visibly when SENT/non-trading-day state has not been committed by 21:30.
    log({ date: day, phase, status: "DELIVERY_NOT_CONFIRMED", run_id: active?.id || runs[0]?.id || null });
    throw new Error(`Delivery not confirmed for ${day}; check GitHub Actions and screener-data`);
  }
  if (active) {
    const result = { date: day, phase, status: "RUN_ACTIVE", run_id: active.id };
    log(result);
    return result;
  }
  // Only recover a missing trigger. Failed/cancelled/completed unsent runs need
  // investigation, rather than an automatic fresh analysis or quota retry.
  if (runs.some(run => !run.other_active)) {
    const result = { date: day, phase, status: "RUN_EXISTS", run_id: runs[0].id };
    log(result);
    return result;
  }
  const dispatched = await github(env, `actions/workflows/${WORKFLOW}/dispatches`, {
    method: "POST",
    payload: { ref: "main", inputs: { mode: "daily", date: day, send_line: true, scheduler: "cloudflare" } },
  }, fetchFn);
  const result = { date: day, phase, status: "DISPATCHED", run_id: dispatched?.workflow_run_id || null };
  log(result);
  return result;
}

export default {
  async scheduled(event, env) {
    // Returning/awaiting this promise records thrown failures in Cron Events.
    await tick(event, env);
  },
};
