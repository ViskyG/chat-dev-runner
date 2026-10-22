"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { test } = require("node:test");
const { runContract } = require("./run-ui-contract.cjs");

async function fixture(t, script, extra = {}) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "analytics-adapter-test-"));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const options = { outDir: directory, command: [process.execPath, "-e", script], cwd: directory,
    env: { ...process.env, CONTROL_CENTER_BASE_URL: "http://control-center:8768" },
    provenance: { test: true }, packageScript: "controlled reporter fixture", echo: false, ...extra };
  // Child fixtures are commands, not workers of Node's parent test harness.
  delete options.env.NODE_TEST_CONTEXT;
  return { directory, options, read: () => JSON.parse(fs.readFileSync(path.join(directory, "ui-contract.json"))),
    xml: () => fs.readFileSync(path.join(directory, "ui-contract.junit.xml"), "utf8") };
}

test("a successful released command yields exactly one aggregate testcase", async t => {
  const f = await fixture(t, "require('node:fs').writeSync(1, 'actual output\\n')");
  assert.equal(await runContract(f.options), 0);
  assert.equal(f.read().status, "passed");
  assert.equal(f.read().complete, true);
  assert.match(f.xml(), /tests="1" failures="0" errors="0"/);
  assert.match(f.xml(), /npm run test:ui \(aggregate\)/);
  assert.match(fs.readFileSync(path.join(f.directory, "ui-contract.log"), "utf8"), /actual output/);
});

test("test failure preserves real exit and bounded original log", async t => {
  const f = await fixture(t, "require('node:fs').writeSync(1, 'x'.repeat(200));process.exitCode=7", { maxLogBytes: 16 });
  assert.equal(await runContract(f.options), 7);
  assert.equal(f.read().exit_code, 7);
  assert.equal(f.read().complete, true);
  assert.equal(f.read().log.stored_bytes, 16);
  assert.equal(f.read().log.truncated, true);
  assert.match(f.xml(), /failures="1" errors="0"/);
});

test("missing executable is an incomplete error, never a passed contract", async t => {
  const f = await fixture(t, "", { command: ["/missing-analytics-adapter-executable"] });
  assert.equal(await runContract(f.options), 1);
  assert.equal(f.read().started, false);
  assert.equal(f.read().complete, false);
  assert.equal(f.read().spawn_error, "ENOENT");
  assert.match(f.xml(), /errors="1"/);
});

test("interruption forwards to command group and cannot produce green evidence", async t => {
  const f = await fixture(t, "setInterval(()=>{},1000)");
  const result = runContract(f.options);
  const deadline = Date.now() + 2000;
  while (!f.read().started && Date.now() < deadline) await new Promise(resolve => setTimeout(resolve, 10));
  assert.equal(f.read().started, true);
  assert.match(f.xml(), /tests="0"/);
  process.emit("SIGTERM");
  assert.equal(await result, 143);
  assert.equal(f.read().status, "interrupted");
  assert.equal(f.read().complete, false);
  assert.match(f.xml(), /errors="1"/);
});
