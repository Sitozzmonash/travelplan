// Run: node --test frontend/tests/guided-sync.test.cjs (from the repository root).
// Transpile the real client component/data outlet in memory; no browser, server or emitted files.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");
const ts = require("../node_modules/typescript");

const root = path.resolve(__dirname, "..");
const compiled = new Map();
const clone = (value) => JSON.parse(JSON.stringify(value));

function session(id = "session-1", extra = {}) {
  return {
    session_id: id, status: "READY", discovery_status: "READY",
    preferences: {}, poi_selections: {}, ...extra,
  };
}

function harness() {
  const hooks = [];
  const timers = new Map();
  const requests = [];
  const routes = [];
  let cursor = 0;
  let nextTimer = 1;
  let effects = [];
  let tree;
  const react = {
    useState(initial) {
      const index = cursor++;
      if (!hooks[index]) hooks[index] = { value: typeof initial === "function" ? initial() : initial };
      return [hooks[index].value, (value) => {
        hooks[index].value = typeof value === "function" ? value(hooks[index].value) : value;
      }];
    },
    useRef(initial) {
      const index = cursor++;
      if (!hooks[index]) hooks[index] = { current: initial };
      return hooks[index];
    },
    useEffect(effect, deps) {
      const index = cursor++;
      const previous = hooks[index];
      if (!previous || deps.some((dep, i) => !Object.is(dep, previous.deps[i]))) {
        hooks[index] = { deps, cleanup: previous?.cleanup };
        effects.push(() => {
          hooks[index].cleanup?.();
          hooks[index].cleanup = effect();
        });
      }
    },
  };
  const jsx = (type, props) => ({ type, props: props ?? {} });
  class ApiError extends Error {
    constructor(message, kind) { super(message); this.kind = kind; }
  }
  const context = vm.createContext({
    console, AbortController, DOMException,
    setTimeout(callback, delay) {
      const id = nextTimer++;
      timers.set(id, { callback, delay });
      return id;
    },
    clearTimeout(id) { timers.delete(id); },
    fetch(url, options) {
      // This is the only transport: never delegate to global fetch.
      assert.ok(url.startsWith("http://guided.test/"));
      return new Promise((resolve, reject) => {
        const request = {
          method: options.method,
          pathname: new URL(url).pathname,
          body: options.body === undefined ? undefined : JSON.parse(options.body),
          done: false,
          respond(payload, status = 200) {
            assert.equal(request.done, false, "request may only settle once");
            request.done = true;
            resolve({ ok: status >= 200 && status < 300, status, text: async () => JSON.stringify(payload) });
          },
          fail() {
            assert.equal(request.done, false);
            request.done = true;
            reject(new Error("simulated network failure"));
          },
        };
        requests.push(request);
        if (request.method === "DELETE") request.respond({});
      });
    },
  });
  const cache = new Map();
  const modules = {
    "./draft": "components/guided/draft.ts",
    "./options": "components/guided/options.ts",
    "@/lib/format": "lib/format.ts",
    "@/lib/sessions": "lib/sessions.ts",
    "@/types/session": "types/session.ts",
  };
  const ui = {
    "@/components/ui/button": ["Button"],
    "@/components/section-card": ["SectionCard"],
    "@/components/state-views": ["ErrorState", "InlineWarning", "PartialNotice"],
    "./discovery-research": ["DiscoveryResearch"],
    "./step-poi": ["StepPoi"],
    "./step-preferences": ["StepPreferences"],
    "./summary-panel": ["SummaryPanel"],
  };
  function requireLocal(name) {
    if (modules[name]) return load(modules[name]);
    if (ui[name]) return Object.fromEntries(ui[name].map((key) => [key, key]));
    if (name === "react") return react;
    if (name === "react/jsx-runtime") return { jsx, jsxs: jsx, Fragment: "Fragment" };
    if (name === "next/navigation") return { useRouter: () => ({ push: (route) => routes.push(route) }) };
    if (name === "next/link") return { default: "Link", __esModule: true };
    if (name === "lucide-react") return new Proxy({}, { get: (_, key) => String(key) });
    if (name === "@/lib/utils") return { cn: (...parts) => parts.filter(Boolean).join(" ") };
    if (name === "@/lib/api") return {
      API_BASE_URL: "http://guided.test", USE_MOCK_API: false, ApiError,
      apiErrorKind: (error) => error.kind,
    };
    if (name === "./wizard-progress") return {
      WizardProgress: "WizardProgress",
      STEP_META: [{ title: "探索确认" }, { title: "偏好" }],
    };
    throw new Error(`Unexpected dependency: ${name}`);
  }
  function load(relative) {
    if (cache.has(relative)) return cache.get(relative).exports;
    if (!compiled.has(relative)) {
      const filename = path.join(root, relative);
      compiled.set(relative, ts.transpileModule(fs.readFileSync(filename, "utf8"), {
        fileName: filename,
        compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020, jsx: ts.JsxEmit.ReactJSX },
      }).outputText);
    }
    const module = { exports: {} };
    cache.set(relative, module);
    vm.runInContext(`(function(require, module, exports) {\n${compiled.get(relative)}\n})`, context, {
      filename: path.join(root, relative),
    })(requireLocal, module, module.exports);
    return module.exports;
  }
  const { GuidedWizard } = load("components/guided/guided-wizard.tsx");
  function render() {
    cursor = 0;
    effects = [];
    tree = GuidedWizard({ initialBasic: { origin: "北京", destination: "成都", startDate: "2026-10-01" } });
    effects.forEach((effect) => effect());
  }
  function nodes(node) {
    if (Array.isArray(node)) return node.flatMap((item) => nodes(item));
    if (!node || typeof node !== "object") return [];
    return [node, ...nodes(node.props?.children)];
  }
  function all(name) {
    return nodes(tree).filter((node) => node.type === name || node.type?.name === name).map((node) => node.props);
  }
  function props(name) {
    const result = all(name)[0];
    assert.ok(result, `expected ${name} in the rendered wizard`);
    return result;
  }
  function primary() {
    const result = all("Button").find((button) => button.className?.includes("min-w-"));
    assert.ok(result);
    return result.onClick;
  }
  function pending(method, suffix) {
    const result = requests.find((request) => !request.done && request.method === method && (!suffix || request.pathname.endsWith(suffix)));
    assert.ok(result, `expected pending ${method} ${suffix ?? ""}`);
    return result;
  }
  // Explicit renders preserve stale handler closures. Microtasks are drained without real time/sleep.
  async function flush() {
    for (let i = 0; i < 20; i++) { await Promise.resolve(); render(); }
  }
  function firePoiTimer() {
    for (const [id, timer] of [...timers]) {
      if (timer.delay === 700) { timers.delete(id); timer.callback(); }
    }
  }
  async function boot(extra = {}) {
    render();
    pending("POST").respond(session("session-1", extra));
    await flush();
  }
  async function preferences() {
    primary()();
    await flush();
    pending("PATCH").respond(session());
    await flush();
    return props("StepPreferences");
  }
  return {
    boot, flush, props, all, primary, pending, firePoiTimer, preferences, requests, routes,
    patches: () => requests.filter((request) => request.method === "PATCH"),
    starts: () => requests.filter((request) => request.pathname.endsWith("/start")),
    draft: load("components/guided/draft.ts"),
  };
}

test("final PATCH failure never STARTs and leaves the two-step wizard retryable", async () => {
  const h = harness();
  await h.boot();
  assert.equal(h.props("WizardProgress").current, 0);
  await h.preferences();
  h.primary()();
  await h.flush();
  h.pending("PATCH").fail();
  await h.flush();
  assert.equal(h.starts().length, 0);
  assert.equal(h.props("WizardProgress").current, 1);
  assert.equal(h.props("fieldset").disabled, false);
  assert.ok(h.all("ErrorState").some((error) => error.title === "没能开始规划"));
});

test("retry uses latest ref draft, never replays an older failed preference PATCH", async () => {
  const h = harness();
  await h.boot();
  const preference = await h.preferences();
  preference.onTransport({ mode: "train" });
  h.primary()(); // no render after changing the draft
  await h.flush();
  assert.equal(h.pending("PATCH").body.transport_mode, "train");
  h.pending("PATCH").fail();
  await h.flush();
  const retry = h.all("ErrorState").find((error) => error.title === "没能开始规划").onRetry;
  h.props("StepPreferences").onTransport({ mode: "flight", priority: "fastest" });
  retry();
  retry(); // same stale render, before React can disable anything
  await h.flush();
  const latest = h.pending("PATCH");
  assert.equal(latest.body.transport_mode, "flight");
  assert.equal(latest.body.transport_priority, "fastest");
  latest.respond(session());
  await h.flush();
  assert.equal(h.patches().length, 3); // continue, failed confirm, fresh confirm; no replay
  assert.equal(h.starts().length, 1);
  h.pending("POST", "/start").respond({ run_id: "run-1" });
  await h.flush();
  assert.deepEqual(h.routes, ["/plan/run-1"]);
});

test("in-flight and queued POI finish before full confirmation; pending debounce is cancelled", async () => {
  const h = harness();
  await h.boot();
  const poi = h.props("StepPoi");
  h.primary()();
  await h.flush();
  const continuing = h.pending("PATCH");
  poi.onSelect("a", "MUST");
  h.firePoiTimer(); // queued behind continue
  await h.flush();
  assert.equal(h.patches().length, 1);
  continuing.respond(session());
  await h.flush();
  const activePoi = h.pending("PATCH");
  assert.deepEqual(activePoi.body, { poi_selections: { a: "MUST" } });
  poi.onSelect("b", "WANT");
  h.firePoiTimer(); // queued behind active POI
  poi.onSelect("c", "REJECT"); // debounce not fired
  const finish = h.primary();
  finish();
  finish();
  poi.onSelect("too-late", "MUST"); // stale handler must also respect the final lock
  h.firePoiTimer();
  await h.flush();
  assert.equal(h.patches().length, 2);
  assert.equal(h.starts().length, 0);
  assert.equal(h.props("fieldset").disabled, true);
  activePoi.respond(session());
  await h.flush();
  const queuedPoi = h.pending("PATCH");
  assert.deepEqual(queuedPoi.body, { poi_selections: { a: "MUST", b: "WANT", c: "REJECT" } });
  assert.equal(h.starts().length, 0);
  queuedPoi.respond(session());
  await h.flush();
  const final = h.pending("PATCH");
  assert.equal(final.body.transport_mode, "any");
  assert.equal(final.body.hotel_priority, "auto");
  assert.equal(final.body.pace, "auto");
  assert.deepEqual(final.body.poi_selections, { a: "MUST", b: "WANT", c: "REJECT" });
  final.respond(session());
  await h.flush();
  poi.onBulk("auto");
  h.firePoiTimer();
  finish();
  await h.flush();
  assert.equal(h.patches().length, 4);
  assert.equal(h.starts().length, 1);
  h.pending("POST", "/start").respond({ run_id: "only-once" });
  await h.flush();
  finish();
  await h.flush();
  assert.equal(h.starts().length, 1);
  assert.equal(h.patches().length, 4);
});

test("sync-error retry is single-flight and uses current POI plus preferences", async () => {
  const h = harness();
  await h.boot();
  const poi = h.props("StepPoi");
  poi.onSelect("old", "MUST");
  h.firePoiTimer();
  await h.flush();
  h.pending("PATCH").fail();
  await h.flush();
  const retry = h.all("Button").find((button) => button.size === "sm").onClick;
  poi.onSelect("old", null);
  poi.onSelect("new", "WANT");
  retry();
  retry();
  h.primary()(); // continue cannot race the retry before a render
  await h.flush();
  assert.equal(h.patches().length, 2);
  assert.deepEqual(h.pending("PATCH").body.poi_selections, { new: "WANT" });
  assert.equal(h.pending("PATCH").body.hotel_room_type, null);
  h.pending("PATCH").respond(session());
  await h.flush();
  h.firePoiTimer();
  await h.flush();
  assert.equal(h.patches().length, 2);
  assert.equal(h.props("WizardProgress").current, 0);
});

test("cleared fields propagate through the real HTTP serializer, including START-error retry", async () => {
  const h = harness();
  await h.boot();
  const poi = h.props("StepPoi");
  poi.onSelect("old", "MUST");
  const preference = await h.preferences();
  preference.onTransport({ mode: "train", priority: "cheapest", constraints: ["no_early"] });
  preference.onHotel({ priority: "value", maxPriceText: "500", roomType: "双床房" });
  preference.onPace("packed");
  h.primary()();
  await h.flush();
  const first = h.pending("PATCH");
  assert.equal(first.body.hotel_max_price_per_night, 500);
  assert.equal(first.body.hotel_room_type, "双床房");
  first.respond(session());
  await h.flush();
  h.pending("POST", "/start").fail();
  await h.flush();
  const retry = h.all("ErrorState").find((error) => error.title === "没能开始规划").onRetry;
  preference.onTransport({ mode: null, priority: null, constraints: [] });
  preference.onHotel({ priority: null, maxPriceText: "", maxPriceSentinel: null, roomType: null });
  preference.onPace("auto");
  poi.onBulk("auto");
  retry();
  retry();
  await h.flush();
  assert.deepEqual(h.pending("PATCH").body, {
    transport_mode: "any", transport_priority: "auto", transport_constraints: ["any_time"],
    hotel_priority: "auto", hotel_max_price_per_night: null, hotel_room_type: null,
    pace: "auto", poi_selections: {},
  });
  h.pending("PATCH").respond(session());
  await h.flush();
  assert.equal(h.starts().length, 2); // one failed attempt, one deliberate retry
  h.pending("POST", "/start").respond({ run_id: "recovered" });
  await h.flush();
  h.firePoiTimer();
  assert.equal(h.patches().length, 3);
  assert.deepEqual(h.routes, ["/plan/recovered"]);
});

for (const outcome of ["success", "failure"]) {
  test(`rebuild ignores old PATCH ${outcome} and drops old queued POI`, async () => {
    const h = harness();
    await h.boot({ status: "DISCOVERING", discovery_status: "DISCOVERING" });
    const poi = h.props("StepPoi");
    poi.onSelect("old-a", "MUST");
    h.firePoiTimer();
    await h.flush();
    const oldPatch = h.pending("PATCH");
    poi.onSelect("old-b", "WANT");
    h.firePoiTimer();
    h.pending("GET").respond(session("session-1", { status: "EXPIRED" }));
    await h.flush();
    h.props("SessionUnusableNotice").onRestart();
    await h.flush();
    assert.equal(h.props("StepPoi").session, null);
    h.pending("POST").respond(session("session-2"));
    await h.flush();
    h.props("StepPoi").onSelect("new", "MUST");
    h.firePoiTimer();
    await h.flush();
    assert.ok(h.pending("PATCH").pathname.endsWith("session-1"));
    if (outcome === "success") oldPatch.respond(session("session-1"));
    else oldPatch.fail();
    await h.flush();
    assert.equal(h.props("StepPoi").session.session_id, "session-2");
    assert.deepEqual(clone(h.props("StepPoi").selections), { new: "MUST" });
    assert.equal(h.patches().filter((request) => request.pathname.endsWith("session-1")).length, 1);
    const newPatch = h.pending("PATCH");
    assert.ok(newPatch.pathname.endsWith("session-2"));
    assert.deepEqual(newPatch.body, { poi_selections: { new: "MUST" } });
    newPatch.respond(session("session-2"));
    await h.flush();
    assert.equal(h.all("Button").some((button) => button.size === "sm"), false);
  });
}

test("rebuild ignores old polling response and serializes preference replay with new POI", async () => {
  const h = harness();
  const discovering = { status: "DISCOVERING", discovery_status: "DISCOVERING" };
  await h.boot(discovering);
  const oldGet = h.pending("GET");
  h.primary()();
  await h.flush();
  h.pending("PATCH").respond(session("session-1", discovering));
  await h.flush();
  const preference = h.props("StepPreferences");
  preference.onTransport({ mode: "train" });
  h.props("WizardProgress").onJump(0);
  await h.flush();
  h.props("StepPoi").onRetryDiscovery();
  await h.flush();
  const recentGet = h.requests.filter((request) => request.method === "GET").at(-1);
  assert.notEqual(recentGet, oldGet);
  recentGet.respond(session("session-1", { status: "EXPIRED" }));
  await h.flush();
  const restart = h.props("SessionUnusableNotice").onRestart;
  restart();
  restart();
  await h.flush();
  assert.equal(h.requests.filter((request) => request.method === "POST").length, 2);
  preference.onTransport({ mode: "flight" }); // latest preference while create is in flight
  h.pending("POST").respond(session("session-2"));
  await h.flush();
  const replay = h.pending("PATCH");
  assert.equal(replay.body.transport_mode, "flight");
  assert.equal("poi_selections" in replay.body, false);
  h.props("StepPoi").onSelect("new", "WANT");
  h.firePoiTimer();
  await h.flush();
  assert.equal(h.patches().length, 2); // continue + replay, POI is still queued
  oldGet.respond(session("session-1"));
  await h.flush();
  assert.equal(h.props("StepPoi").session.session_id, "session-2");
  replay.respond(session("session-2"));
  await h.flush();
  const poi = h.pending("PATCH");
  assert.ok(poi.pathname.endsWith("session-2"));
  assert.deepEqual(poi.body, { poi_selections: { new: "WANT" } });
  assert.deepEqual(clone(h.props("StepPoi").selections), { new: "WANT" });
  poi.respond(session("session-2"));
  await h.flush();
});

test("lost START response recovers the existing run without submitting a second START", async () => {
  const h = harness();
  await h.boot();
  await h.preferences();
  h.primary()();
  await h.flush();
  h.pending("PATCH").respond(session());
  await h.flush();
  h.pending("POST", "/start").fail(); // server accepted it, but the response never arrived
  await h.flush();
  const retry = h.all("ErrorState").find((error) => error.title === "没能开始规划").onRetry;
  retry();
  await h.flush();
  h.pending("PATCH").respond(session("session-1", { status: "STARTING", run_id: "accepted-run" }));
  await h.flush();
  assert.equal(h.starts().length, 1);
  assert.deepEqual(h.routes, ["/plan/accepted-run"]);
  retry();
  await h.flush();
  assert.equal(h.starts().length, 1);
});

test("full draft preserves explicit sentinels while nullable hotel resets are present", () => {
  const h = harness();
  const draft = h.draft.createEmptyDraft();
  for (const sentinel of ["auto", "undecided", "unlimited"]) {
    draft.hotel.maxPriceSentinel = sentinel;
    draft.hotel.roomType = sentinel;
    const patch = clone(h.draft.fullDraftPatch(draft));
    assert.equal(patch.hotel_max_price_per_night, sentinel);
    assert.equal(patch.hotel_room_type, sentinel);
    assert.deepEqual(patch.poi_selections, {});
  }
  draft.hotel.maxPriceSentinel = null;
  draft.hotel.roomType = null;
  const patch = clone(h.draft.fullDraftPatch(draft));
  assert.equal(patch.hotel_max_price_per_night, null);
  assert.equal(patch.hotel_room_type, null);
});
