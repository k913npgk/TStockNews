export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.hostname !== "api.github.com" || request.method !== "GET" ||
        request.headers.get("Authorization") !== "Bearer runtime-test-only") {
      return new Response(null, { status: 401 });
    }
    if (env.CASE === "redirect") return new Response(null, { status: 307, headers: { Location: "https://invalid.example" } });
    if (!url.pathname.includes("contents/data/delivery/2026-10-05.json")) return new Response(null, { status: 500 });
    return Response.json({ encoding: "base64", content: btoa(JSON.stringify({ status: "SENT" })) });
  },
};
