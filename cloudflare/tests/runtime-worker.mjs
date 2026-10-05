import { tick } from "../worker.mjs";

export default {
  async scheduled(event, env) {
    // Local binding exercises Workers' native fetch option parsing without
    // network, production data, or real GitHub credentials.
    await tick(event, env, { fetchFn: (url, options) => env.GITHUB_API.fetch(url, options) });
  },
};
