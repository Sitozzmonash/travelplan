// Run: node --test frontend/tests/guided-sync.test.cjs (from the repository root).
// Transpile the real client component/data outlet in memory; no browser, server or emitted files.
/* eslint-disable @typescript-eslint/no-require-imports -- Node's existing .cjs test harness uses CommonJS. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");
const ts = require("../node_modules/typescript");
/* eslint-enable @typescript-eslint/no-require-imports */

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
    "@/components/state-views": ["ErrorState", "InlineWarning", "PartialNotice", "EmptyState", "LoadingState"],
    "@/components/ui/skeleton": ["Skeleton"],
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
    const loadedModule = { exports: {} };
    cache.set(relative, loadedModule);
    vm.runInContext(`(function(require, module, exports) {\n${compiled.get(relative)}\n})`, context, {
      filename: path.join(root, relative),
    })(requireLocal, loadedModule, loadedModule.exports);
    return loadedModule.exports;
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
  async function preferences(extra = {}) {
    primary()();
    await flush();
    pending("PATCH").respond(session("session-1", extra));
    await flush();
    return props("StepPreferences");
  }
  return {
    boot, flush, props, all, primary, pending, firePoiTimer, preferences, requests, routes,
    patches: () => requests.filter((request) => request.method === "PATCH"),
    starts: () => requests.filter((request) => request.pathname.endsWith("/start")),
    draft: load("components/guided/draft.ts"),
    sessions: load("lib/sessions.ts"),
    options: load("components/guided/options.ts"),
    component(relative, name, props) {
      function expand(node) {
        if (Array.isArray(node)) return node.map(expand);
        if (!node || typeof node !== "object") return node;
        if (typeof node.type === "function") return expand(node.type(node.props));
        return { ...node, props: { ...node.props, children: expand(node.props.children) } };
      }
      const rendered = expand(load(relative)[name](props));
      const elements = nodes(rendered);
      function text(node) {
        if (Array.isArray(node)) return node.map(text).join(" ");
        return node && typeof node === "object" ? text(node.props?.children) : typeof node === "string" || typeof node === "number" ? String(node) : "";
      }
      return { nodes: elements, text: text(rendered) };
    },
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

function recommended(extra = {}) {
  return {
    recommendation: { status: "READY", source: "llm", version: 1, place_ids: ["park"] },
    place_candidates: [{ place_id: "park", name: "人民公园", category: "attraction", district: "青羊区", evidence_count: 3 }],
    ...extra,
  };
}

function poiComponent(h, extra, props = {}) {
  return h.component("components/guided/step-poi.tsx", "StepPoi", {
    session: h.sessions.normalizeSession(session("s", extra)), settled: false, unusable: false,
    pollStalled: false, selections: {}, expanded: [], bulk: null,
    onSelect() {}, onBulk() {}, onToggleExpand() {}, onRetryDiscovery() {}, ...props,
  });
}

function researchComponent(h, extra) {
  return h.component("components/guided/discovery-research.tsx", "DiscoveryResearch", {
    session: h.sessions.normalizeSession(session("s", extra)), settled: false,
  });
}

function primaryDisabled(h) {
  return h.all("Button").find((button) => button.className?.includes("min-w-")).disabled;
}

test("normalization preserves optional recommendation, stage states and card identity metadata", () => {
  const h = harness();
  const legacy = h.sessions.normalizeSession(session());
  assert.equal(legacy.recommendation, undefined);
  assert.equal(h.options.recommendationStartIssue(legacy, {}), null);
  const current = h.sessions.normalizeSession(session("s", recommended({
    discovery_status: "RUNNING", discovery: { database: { status: "OK", result_count: "4" } },
    place_candidates: [{ place_id: "park", name: "公园", display_name: "公园（青羊区）", canonical_place_id: "canonical", district: "青羊区", merged_from: ["alias", null], evidence_count: "2", reason: "适合散步" }],
  })));
  assert.equal(current.discovery_status, "RUNNING");
  assert.equal(current.discovery.database.result_count, 4);
  assert.equal(current.recommendation.version, 1);
  assert.equal(current.place_candidates[0].display_name, "公园（青羊区）");
  assert.equal(current.place_candidates[0].canonical_place_id, "canonical");
  assert.deepEqual(clone(current.place_candidates[0].merged_from), ["alias"]);
  assert.equal(current.place_candidates[0].reason, "适合散步");
  assert.equal(current.place_candidates[0].evidence_count, 2);
  assert.equal(h.sessions.isDiscoverySettled(current), false);
  assert.equal(h.sessions.normalizeSession(session("s", { discovery_status: "PENDING" })).discovery_status, "PENDING");
  const malformed = h.sessions.normalizeSession(session("s", { recommendation: {}, place_candidates: current.place_candidates }));
  assert.equal(h.options.discoveryPlaces(malformed).length, 0);
  assert.ok(h.options.recommendationStartIssue(malformed, {}));
});

test("new cards never fall back to raw pools; dedupe uses IDs only, not names or evidence aliases", () => {
  const h = harness();
  const cards = [
    { place_id: "a", name: "人民公园", district: "青羊区", canonical_place_id: "c", merged_from: ["source-1"] },
    { place_id: "b", name: "人民公园", district: "双流区", merged_from: ["source-1"] },
    { place_id: "c", name: "人民公园正门" },
    { place_id: "d", name: "人民公园餐厅" },
  ];
  const view = h.sessions.normalizeSession(session("s", recommended({
    recommendation: { status: "READY", place_ids: ["a", "b", "c", "d"] },
    place_candidates: cards,
    poi_pools: { attraction: [{ place_id: "raw", name: "未经筛选的候选" }] },
  })));
  assert.deepEqual(clone(h.options.discoveryPlaces(view).map((place) => place.place_id)), ["a", "b", "d"]);
  assert.equal(h.options.placeDisplayName(cards[0]), "人民公园（青羊区）");
  assert.equal(h.options.placeDisplayName({ ...cards[0], display_name: "后端消歧名" }), "后端消歧名");
  assert.equal("raw" in h.options.bestValueSelections(view), false);
  view.recommendation.place_ids = [];
  assert.equal(h.options.discoveryPlaces(view).length, 0);
  delete view.recommendation;
  assert.deepEqual(clone(h.options.discoveryPlaces(view).map((place) => place.place_id)), ["raw"]);
});

test("research shows the guide-library stages only, keeps legacy guide events, and stops all busy text on failure", () => {
  const h = harness();
  const running = researchComponent(h, {
    discovery_status: "RUNNING", recommendation: { status: "PENDING", place_ids: [] },
    discovery: { database: { status: "OK", result_count: 4 } },
  });
  assert.match(running.text, /数据库攻略：检索结束，4 条结果/);
  assert.match(running.text, /推荐生成：等待处理/);
  // 联网补充阶段已从后端契约移除：既没有这一行，也不会把任何状态说成"正在搜索网页"。
  assert.doesNotMatch(running.text, /Web 补充|网页|联网/);
  assert.doesNotMatch(running.text, /正在查询交通|正在比较酒店|已完成/);
  for (const event of ["social", "social_discovery_finished", "database_finished"]) {
    const result = researchComponent(h, { events: [{ event, detail: "status=OK，结果 5 条" }] });
    assert.match(result.text, /数据库攻略：检索结束，5 条结果/);
    assert.doesNotMatch(result.text, /正在/);
  }
  for (const status of ["READY", "PARTIAL", "FAILED"]) {
    const result = researchComponent(h, {
      discovery_status: status, recommendation: { status, place_ids: [] },
      discovery: { database: { status: "RUNNING" }, web: { status: "PENDING" } },
    });
    assert.doesNotMatch(result.text, /正在|等待处理|已就绪/);
    assert.doesNotMatch(result.text, /Web 补充/);
    assert.match(result.text, /暂无可用推荐/);
  }
  const failedEvent = researchComponent(h, { events: [{ event: "social_discovery_finished", detail: "status=FAILED，结果 0 条" }] });
  assert.match(failedEvent.text, /数据库攻略：未成功获取结果/);
  const zero = researchComponent(h, { discovery: { database: { status: "OK", result_count: 0 }, web: { status: "SKIPPED" } } });
  assert.match(zero.text, /数据库攻略：检索结束，无结果/);
  assert.doesNotMatch(zero.text, /正在|等待处理|Web 补充/);
  const fallback = researchComponent(h, recommended({ recommendation: { status: "PARTIAL", source: "evidence_fallback", place_ids: ["park"] } }));
  assert.match(fallback.text, /证据降级推荐：1 个地点（部分可用）/);
});

test("legacy sessions carrying a retired web stage never crash and never render it as busy", () => {
  const h = harness();
  const running = researchComponent(h, {
    discovery_status: "RUNNING", recommendation: { status: "PENDING", place_ids: [] },
    discovery: { database: { status: "OK", result_count: 4 }, web: { status: "RUNNING" }, web_guides: { status: "RUNNING" } },
    events: [
      { event: "web_supplement_started", detail: "status=RUNNING" },
      { event: "web_discovery_finished", detail: "status=RUNNING，结果 3 条" },
    ],
  });
  assert.match(running.text, /数据库攻略：检索结束，4 条结果/);
  assert.match(running.text, /推荐生成：等待处理/);
  assert.doesNotMatch(running.text, /Web 补充|正在处理|web_supplement|web_discovery/);
  const settled = researchComponent(h, {
    discovery_status: "READY",
    recommendation: { status: "READY", source: "llm", version: 1, place_ids: ["park"] },
    place_candidates: [{ place_id: "park", name: "人民公园", category: "attraction", district: "青羊区" }],
    discovery: { web: { status: "RUNNING" } },
    events: [{ event: "web_supplement_started", detail: "status=RUNNING" }],
  });
  assert.match(settled.text, /推荐 1 个景点和体验/);
  assert.match(settled.text, /数据库攻略：已结束，未返回阶段结果/);
  assert.doesNotMatch(settled.text, /正在|等待处理|Web 补充/);
});

test("cards avoid duplicate busy blocks and skeletons when recommendations exist; empty results stay honest", () => {
  const h = harness();
  const result = poiComponent(h, recommended({ discovery_status: "RUNNING" }));
  assert.equal(result.nodes.some((node) => node.type === "LoadingState" || node.type === "Skeleton"), false);
  assert.match(result.text, /人民公园（青羊区）/);
  assert.match(result.text, /来自 3 篇攻略/);
  assert.match(result.text, /不勾选时使用这份推荐/);
  assert.equal(result.nodes.filter((node) => node.type === "DiscoveryResearch").length, 1);
  for (const evidence_count of [undefined, null, 0, -1, "invalid"]) {
    const card = poiComponent(h, recommended({ place_candidates: [{ place_id: "park", name: "人民公园", display_name: "人民公园·双流", district: "双流区", area: "商圈", evidence_count }] }));
    assert.match(card.text, /暂无关联攻略/);
    assert.match(card.text, /人民公园·双流/);
    assert.match(card.text, /双流区 · 商圈/);
    assert.doesNotMatch(card.text, /来自 0 篇攻略/);
  }
  const waiting = poiComponent(h, { discovery_status: "RUNNING", recommendation: { status: "PENDING", place_ids: [] } });
  assert.ok(waiting.nodes.some((node) => node.type === "Skeleton"));
  const empty = poiComponent(h, recommended({ recommendation: { status: "FAILED", place_ids: [] } }));
  assert.equal(empty.nodes.some((node) => node.type === "Skeleton"), false);
  assert.match(empty.nodes.find((node) => node.type === "EmptyState").props.hint, /重新生成可用推荐/);
  const hotel = h.component("components/guided/discovery-research.tsx", "HotelAreaRecommendations", { areas: [{ key: "center", name: "市中心", tags: [] }] });
  assert.match(hotel.text, /根据攻略与游玩范围推荐/);
  assert.doesNotMatch(hotel.text, /已按你的酒店策略排序/);
});

test("legacy tool-named guide stages still resolve while retired web stages stay hidden", () => {
  const h = harness();
  const result = researchComponent(h, {
    discovery_status: "RUNNING", recommendation: { status: "PENDING", place_ids: [] },
    discovery: {
      recall_city_guides: { status: "CACHE", result_count: 7 },
      recommendation: { status: "RUNNING" },
    },
  });
  assert.match(result.text, /数据库攻略：检索结束，7 条结果/);
  assert.match(result.text, /推荐生成：正在处理/);
  assert.doesNotMatch(result.text, /Web 补充|联网|网页/);
  const draft = h.draft.createEmptyDraft();
  draft.poi.selections = { park: "MUST" };
  const summary = h.draft.summarize(draft, h.sessions.normalizeSession(session("s", recommended())));
  assert.deepEqual(clone(summary.must.names), ["人民公园（青羊区）"]);
});

test("legacy sessions without recommendation retain the historical START behavior even with no discovery results", async () => {
  const h = harness();
  const extra = { discovery_status: "FAILED", place_candidates: [] };
  await h.boot(extra);
  await h.preferences(extra);
  assert.equal(primaryDisabled(h), false);
  h.primary()();
  await h.flush();
  h.pending("PATCH").respond(session("session-1", extra));
  await h.flush();
  assert.equal(h.starts().length, 1);
});

for (const status of ["PENDING", "RUNNING"]) {
  test(`${status} recommendation permits preferences but blocks START, including a direct stale handler`, async () => {
    const h = harness();
    const extra = recommended({ discovery_status: "RUNNING", recommendation: { status, place_ids: [] } });
    await h.boot(extra);
    assert.equal(primaryDisabled(h), false);
    await h.preferences(extra);
    assert.equal(h.props("WizardProgress").current, 1);
    assert.equal(primaryDisabled(h), true);
    assert.ok(h.all("InlineWarning").some((warning) => /推荐尚未就绪/.test(warning.description)));
    h.primary()();
    await h.flush();
    assert.equal(h.patches().length, 1);
    assert.equal(h.starts().length, 0);
    h.pending("GET").respond(session("session-1", recommended()));
    await h.flush();
    assert.equal(primaryDisabled(h), false);
    h.primary()();
    await h.flush();
    h.pending("PATCH").respond(session("session-1", recommended()));
    await h.flush();
    assert.equal(h.starts().length, 1);
  });
}

for (const extra of [
  recommended({ recommendation: { status: "FAILED", place_ids: [] } }),
  recommended({ recommendation: { status: "READY", place_ids: [] } }),
  recommended({ place_candidates: [] }),
]) {
  test(`no usable recommendation IDs blocks formal start (${JSON.stringify(extra)})`, async () => {
    const h = harness();
    await h.boot(extra);
    await h.preferences(extra);
    assert.equal(primaryDisabled(h), true);
    h.primary()();
    await h.flush();
    assert.equal(h.starts().length, 0);
    assert.equal(h.props("fieldset").disabled, false);
    h.props("WizardProgress").onJump(0);
    await h.flush();
    h.props("StepPoi").onRetryDiscovery();
    await h.flush();
    assert.equal(h.requests.filter((request) => request.method === "POST").length, 2);
  });
}

test("final PATCH recommendation is rechecked and the existing final lock is released on rejection", async () => {
  const h = harness();
  await h.boot(recommended());
  await h.preferences(recommended());
  h.primary()();
  await h.flush();
  h.pending("PATCH").respond(session("session-1", recommended({ recommendation: { status: "FAILED", place_ids: [] } })));
  await h.flush();
  assert.equal(h.starts().length, 0);
  assert.equal(h.props("fieldset").disabled, false);
  assert.equal(primaryDisabled(h), true);
  const error = h.all("ErrorState").find((item) => item.title === "没能开始规划");
  assert.equal(error.retryLabel, "返回探索页");
  error.onRetry();
  await h.flush();
  assert.equal(h.props("WizardProgress").current, 0);
});

test("all-rejected and stale selections block start; auto clears them and uses only this recommendation", async () => {
  const h = harness();
  const view = h.sessions.normalizeSession(session("s", recommended()));
  assert.match(h.options.recommendationStartIssue(view, { park: "REJECT" }), /没有可安排/);
  assert.match(h.options.recommendationStartIssue(view, { raw: "WANT" }), /不在当前推荐/);
  assert.equal(h.options.recommendationStartIssue(view, {}), null);
  await h.boot(recommended());
  const poi = h.props("StepPoi");
  poi.onSelect("park", "REJECT");
  await h.preferences(recommended());
  assert.equal(primaryDisabled(h), true);
  poi.onBulk("auto");
  await h.flush();
  assert.equal(primaryDisabled(h), false);
  h.primary()();
  await h.flush();
  assert.deepEqual(h.pending("PATCH").body.poi_selections, {});
  h.pending("PATCH").respond(session("session-1", recommended()));
  await h.flush();
  assert.equal(h.starts().length, 1);
});

for (const [code, expected] of [
  ["recommendation_not_ready", /推荐还在生成/],
  ["recommendation_unavailable", /没有可用推荐/],
  ["no_selected_places", /没有可安排的地点/],
  ["invalid_place_selection", /已不在当前推荐/],
]) {
  test(`real START HTTP response maps ${code} to actionable Chinese`, async () => {
    const h = harness();
    await h.boot(recommended());
    await h.preferences(recommended());
    h.primary()();
    await h.flush();
    h.pending("PATCH").respond(session("session-1", recommended()));
    await h.flush();
    h.pending("POST", "/start").respond({ detail: { code, message: "backend message" } }, 409);
    await h.flush();
    const error = h.all("ErrorState").find((item) => item.title === "没能开始规划");
    assert.match(error.description, expected);
    assert.match(error.description, /返回探索页/);
    assert.equal(h.props("fieldset").disabled, false);
  });
}
