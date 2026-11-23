const assert = require("node:assert/strict");
const { test } = require("node:test");
const { extractFunctions, fakeDocument, FakeElement } = require("./frontend-harness.cjs");

function text(item) {
  return typeof item === "string" ? item : item.textContent;
}

const NAMES = ["ROUTE_VISIT_PAGE", "PACE_SPORTS", "routeVisitDate", "routeVisitClock", "routeVisitNumber", "routeVisitOutput", "routeVisitChart", "routeVisitTable", "renderRoute"];
const FORMAT = {
  number: (value, { digits }) => Number(value).toFixed(digits).replace(".", ","),
  clock: (seconds) => { const t = Math.round(seconds); const hh = Math.floor(t / 3600); const mm = Math.floor((t % 3600) / 60); const ss = String(t % 60).padStart(2, "0"); return hh ? `${hh}:${String(mm).padStart(2, "0")}:${ss}` : `${mm}:${ss}`; },
};

function loadRenderRoute(charts = []) {
  const document = fakeDocument();
  // The chart comes from analysis.js; the stub records what it was asked to draw.
  const analysisChart = (title, series, unit) => { charts.push({ title, unit, values: series[0].points.map((point) => point.value) }); return new FakeElement("section"); };
  return extractFunctions("activity-details.js", NAMES, { document, AppFormat: FORMAT, analysisChart }).renderRoute;
}

const ROUTE = { label: "Hausrunde", repeat_index: 5, repeat_count: 6, route_id: "r3" };
const head = (content) => content.children[0].children[0];

test("the route heading shows its label and visit with a rename action", () => {
  const content = new FakeElement("div");
  loadRenderRoute()(ROUTE, async () => ({}), content);
  const line = head(content);
  assert.equal(text(line.children[0]), "Hausrunde · 5. von 6 Besuchen");
  assert.equal(line.children[1].tag, "button");
  assert.equal(content.children[0].children.length, 1, "no visits, no chart or table");
});

test("a session without a route renders nothing", () => {
  const content = new FakeElement("div");
  loadRenderRoute()(undefined, async () => ({}), content);
  assert.equal(content.children.length, 0);
});

test("renaming posts the route id and name, then shows the new label", async () => {
  const content = new FakeElement("div");
  const calls = [];
  const api = async (url, options) => { calls.push([url, JSON.parse(options.body), options.method]); return { label: "Seerunde" }; };
  loadRenderRoute()(ROUTE, api, content);
  const line = head(content);
  line.children[1].fire("click");
  const form = line.children[0];
  assert.equal(form.tag, "form");
  const input = form.children.find((child) => child.tag === "input");
  assert.equal(input.value, "Hausrunde");
  assert.equal(input.maxLength, 60);
  input.value = "Seerunde";
  await form.fire("submit");
  assert.deepEqual(calls, [["/api/routes/rename", { route_id: "r3", name: "Seerunde" }, "POST"]]);
  assert.equal(text(line.children[0]), "Seerunde · 5. von 6 Besuchen");
});

test("a refused name stays in the form with the message", async () => {
  const content = new FakeElement("div");
  const api = async () => { throw new Error("Der Name ist ungültig."); };
  loadRenderRoute()(ROUTE, api, content);
  const line = head(content);
  line.children[1].fire("click");
  const form = line.children[0];
  await form.fire("submit");
  assert.equal(line.children[0], form);
  assert.match(text(form), /Der Name ist ungültig\./);
});

// #3763: chart first, then every visit in an open toggle, a page of six at a time.
test("visits show as a pace chart and a paged table with the session marked", () => {
  const visits = Array.from({ length: 8 }, (_, index) => ({
    activity_id: `act_${8 - index}`,
    date: `2025-10-0${8 - index}`,
    moving_duration_s: 2400 + index,
    avg_hr: index === 1 ? null : 150 - index,
    avg_speed_mps: index === 2 ? null : 2.5,
    temperature_c: index === 0 ? 12.4 : null,
  }));
  const charts = [];
  const content = new FakeElement("div");
  loadRenderRoute(charts)({ ...ROUTE, repeat_index: 7, repeat_count: 8, visits }, async () => ({}), content, { sport: "run", activity_id: "act_7" });
  const [, chart, toggle] = content.children[0].children;
  assert.equal(chart.tag, "section");
  // Oldest first on the time axis; 2.5 m/s is 400 s/km; a missing speed stays a gap.
  assert.deepEqual(charts, [{ title: "Pace je Besuch", unit: "s/km", values: [400, 400, 400, 400, 400, null, 400, 400] }]);
  assert.equal(toggle.tag, "details");
  assert.equal(toggle.open, true);
  assert.equal(text(toggle.children[0]), "Alle 8 Besuche ansehen");
  const body = toggle.children[1].children[0].children[1];
  assert.deepEqual(body.children[0].children.map(text), ["08.10.2025", "40:00", "150", "6:40", "12"]);
  assert.deepEqual(body.children[1].children.map(text), ["07.10.2025", "40:01", "–", "6:40", "–"]);
  assert.equal(text(body.children[2].children[3]), "–");
  assert.equal(body.children[1].attributes["aria-current"], "true");
  assert.equal(body.children[0].attributes["aria-current"], undefined);
  assert.equal(body.children.length, 6);
  const more = toggle.children[2];
  assert.equal(more.hidden, false);
  more.fire("click");
  assert.equal(body.children.length, 8);
  assert.equal(more.hidden, true);
});

test("a ride shows its speed in km/h", () => {
  const visits = [
    { activity_id: "b", date: "2025-06-02", avg_speed_mps: 8 },
    { activity_id: "a", date: "2025-06-01", avg_speed_mps: 7.5, moving_duration_s: 7530 },
  ];
  const charts = [];
  const content = new FakeElement("div");
  loadRenderRoute(charts)({ ...ROUTE, visits }, async () => ({}), content, { sport: "ride", activity_id: "b" });
  assert.equal(charts[0].unit, "km/h");
  assert.deepEqual(charts[0].values, [27, 28.8]);
  const toggle = content.children[0].children[2];
  const headRow = toggle.children[1].children[0].children[0].children[0];
  assert.equal(text(headRow.children[3]), "km/h");
  assert.equal(text(toggle.children[1].children[0].children[1].children[0].children[3]), "28,8");
  // A visit over an hour reads h:mm:ss (Gitar on #3775).
  assert.equal(text(toggle.children[1].children[0].children[1].children[1].children[1]), "2:05:30");
});

test("a ride on a run's loop shows no pace", () => {
  const visits = [
    { activity_id: "run2", date: "2025-06-03", sport: "run", avg_speed_mps: 2.5 },
    { activity_id: "ride1", date: "2025-06-02", sport: "ride", avg_speed_mps: 8 },
    { activity_id: "run1", date: "2025-06-01", sport: "run", avg_speed_mps: 2.5 },
  ];
  const charts = [];
  const content = new FakeElement("div");
  loadRenderRoute(charts)({ ...ROUTE, visits }, async () => ({}), content, { sport: "run", activity_id: "run2" });
  assert.deepEqual(charts[0].values, [400, null, 400]);
  const body = content.children[0].children[2].children[1].children[0].children[1];
  assert.equal(text(body.children[1].children[3]), "–");
});
