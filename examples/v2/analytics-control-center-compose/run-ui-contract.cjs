"use strict";

// This adapter reports the released command's outcome; it contains no app tests.
const fs = require("node:fs");
const path = require("node:path");
const { spawn } = require("node:child_process");
const { performance } = require("node:perf_hooks");

function writeAtomic(file, value) {
  const temporary = `${file}.tmp`;
  fs.writeFileSync(temporary, value, { mode: 0o600 });
  fs.renameSync(temporary, file);
}

function xml(value) {
  return String(value).replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f]/g, "?")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&apos;");
}

function sample(usage) {
  for (const directory of ["/tmp", "/dev/shm"]) {
    try {
      const stats = fs.statfsSync(directory);
      usage[directory] = { total_bytes: stats.blocks * stats.bsize,
        peak_used_bytes: Math.max(usage[directory]?.peak_used_bytes || 0,
          (stats.blocks - stats.bfree) * stats.bsize) };
    } catch (error) { usage[directory] = { unavailable: error.code }; }
  }
}

function counters() {
  return Object.fromEntries(["memory.events", "memory.peak", "pids.events", "pids.peak"].map(name => {
    try { return [name, fs.readFileSync(`/sys/fs/cgroup/${name}`, "utf8").slice(0, 2048)]; }
    catch { return [name, "unavailable"]; }
  }));
}

async function runContract({ outDir, command, cwd, env, provenance,
  packageScript, maxLogBytes = 4 * 1024 * 1024, echo = true }) {
  fs.mkdirSync(outDir, { recursive: true });
  const started = performance.now();
  const report = { schema_version: "cdr-analytics-ui-result/1", status: "starting",
    started: false, complete: false, argv: command, cwd,
    base_url: env.CONTROL_CENTER_BASE_URL, package_script: packageScript,
    provenance, started_at: new Date().toISOString(),
    junit_scope: "one aggregate released npm run test:ui command; not individual assertion counts",
    resources: {}, counters_before: counters(),
    log: { path: "ui-contract.log", limit_bytes: maxLogBytes, observed_bytes: 0,
      stored_bytes: 0, truncated: false } };
  const publish = () => writeAtomic(path.join(outDir, "ui-contract.json"), JSON.stringify(report, null, 2) + "\n");
  publish();
  // A killed wrapper leaves zero completed tests, never a premature green report.
  writeAtomic(path.join(outDir, "ui-contract.junit.xml"),
    '<testsuite name="released-ui-contract" tests="0" failures="0" errors="0" skipped="0" time="0"/>\n');
  const log = fs.openSync(path.join(outDir, "ui-contract.log"), "wx", 0o600);
  sample(report.resources);
  const timer = setInterval(() => sample(report.resources), 100);
  let child;
  let requestedSignal = null;
  let spawnError = null;
  let killTimer;
  const interrupt = signal => {
    if (requestedSignal) return;
    requestedSignal = signal;
    report.status = "interrupted";
    report.requested_signal = signal;
    publish();
    if (child.pid) {
      try { process.kill(-child.pid, signal); } catch (error) {
        if (error.code !== "ESRCH") throw error;
      }
      killTimer = setTimeout(() => {
        try { process.kill(-child.pid, "SIGKILL"); } catch (error) {
          if (error.code !== "ESRCH") throw error;
        }
      }, 1000);
    }
  };
  const onTerm = () => interrupt("SIGTERM");
  const onInt = () => interrupt("SIGINT");
  process.on("SIGTERM", onTerm);
  process.on("SIGINT", onInt);
  const capture = (buffer, stream) => {
    report.log.observed_bytes += buffer.length;
    const remaining = Math.max(0, maxLogBytes - report.log.stored_bytes);
    const saved = buffer.subarray(0, remaining);
    fs.writeSync(log, saved);
    report.log.stored_bytes += saved.length;
    report.log.truncated = report.log.observed_bytes > report.log.stored_bytes;
    if (echo) stream.write(buffer);
  };
  try {
    child = spawn(command[0], command.slice(1), { cwd, env, detached: true,
      stdio: ["ignore", "pipe", "pipe"] });
    child.stdout.on("data", buffer => capture(buffer, process.stdout));
    child.stderr.on("data", buffer => capture(buffer, process.stderr));
    child.on("spawn", () => { report.started = true; report.status = "running"; publish(); });
    child.on("error", error => { spawnError = error.code || error.name; });
    const { code, signal } = await new Promise(resolve => child.on("close", (code, signal) => resolve({ code, signal })));
    report.complete = report.started && Number.isInteger(code) && !signal && !requestedSignal && !spawnError;
    report.exit_code = code;
    report.signal = signal;
    report.spawn_error = spawnError;
    report.status = requestedSignal ? "interrupted" : spawnError || signal ? "error" : code === 0 ? "passed" : "failed";
    report.duration_seconds = (performance.now() - started) / 1000;
    report.finished_at = new Date().toISOString();
    sample(report.resources);
    report.counters_after = counters();
    publish();
    const passed = report.complete && code === 0;
    const failed = report.complete && code !== 0;
    const detail = `status=${report.status}; exit=${code}; signal=${signal}; requested=${requestedSignal}; spawn=${spawnError}`;
    const result = passed ? "" : `<${failed ? "failure" : "error"} message="${xml(detail)}">See ui-contract.json and ui-contract.log.</${failed ? "failure" : "error"}>`;
    writeAtomic(path.join(outDir, "ui-contract.junit.xml"),
      `<testsuite name="released-ui-contract" tests="1" failures="${failed ? 1 : 0}" errors="${!passed && !failed ? 1 : 0}" skipped="0" time="${report.duration_seconds}">` +
      `<testcase classname="analytics-control-center.release" name="npm run test:ui (aggregate)" time="${report.duration_seconds}">${result}</testcase></testsuite>\n`);
    return requestedSignal ? (requestedSignal === "SIGINT" ? 130 : 143) : report.complete ? code : 1;
  } finally {
    clearInterval(timer);
    clearTimeout(killTimer);
    process.removeListener("SIGTERM", onTerm);
    process.removeListener("SIGINT", onInt);
    fs.closeSync(log);
  }
}

async function main() {
  const provenance = JSON.parse(fs.readFileSync("/src/release-provenance.json", "utf8"));
  const packageJson = JSON.parse(fs.readFileSync("/work/package.json", "utf8"));
  if (typeof packageJson.scripts?.["test:ui"] !== "string") throw new Error("Released image lacks test:ui");
  return runContract({ outDir: "/out", command: ["npm", "run", "test:ui"], cwd: "/work",
    env: { ...process.env, HOME: "/tmp", CONTROL_CENTER_BASE_URL: "http://control-center:8768" },
    provenance, packageScript: packageJson.scripts["test:ui"] });
}

module.exports = { runContract };
if (require.main === module) main().then(code => { process.exitCode = code; }).catch(error => {
  console.error(error);
  process.exitCode = 1;
});
