#!/usr/bin/env node
"use strict";

/**
 * Thin launcher: find a usable python3, then hand over to server.py.
 * Every argument is passed through untouched (--host, --port, --help).
 */

const { spawn, spawnSync } = require("child_process");
const path = require("path");

const MIN_PYTHON = [3, 9];
const SERVER = path.join(__dirname, "..", "server.py");
const PROBE = "import sys; print('%d.%d' % sys.version_info[:2])";

function probe(candidate) {
  const result = spawnSync(candidate, ["-c", PROBE], {
    encoding: "utf8",
    timeout: 10000,
  });
  if (result.error || result.status !== 0) return null;
  const parts = String(result.stdout).trim().split(".");
  const major = Number(parts[0]);
  const minor = Number(parts[1]);
  if (!Number.isInteger(major) || !Number.isInteger(minor)) return null;
  return { version: `${major}.${minor}`, major, minor };
}

function findPython() {
  const candidates = [
    process.env.AGENT_SIGNALS_PYTHON,
    "python3",
    "/usr/bin/python3",
    "/opt/homebrew/bin/python3",
  ].filter(Boolean);

  const tooOld = [];
  for (const candidate of candidates) {
    const found = probe(candidate);
    if (!found) continue;
    if (
      found.major > MIN_PYTHON[0] ||
      (found.major === MIN_PYTHON[0] && found.minor >= MIN_PYTHON[1])
    ) {
      return { command: candidate, version: found.version };
    }
    tooOld.push(`${candidate} (${found.version})`);
  }

  const required = MIN_PYTHON.join(".");
  console.error(
    tooOld.length
      ? `agent-signals: Python ${required}+ required, only found ${tooOld.join(", ")}.`
      : `agent-signals: no python3 found. Install Python ${required}+ (macOS ships one at /usr/bin/python3).`
  );
  console.error(
    "Point AGENT_SIGNALS_PYTHON at a specific interpreter to override the search."
  );
  process.exit(1);
}

function main() {
  if (process.platform !== "darwin") {
    console.error(
      `agent-signals: macOS only (needs osascript and BSD ps), current platform is ${process.platform}.`
    );
    process.exit(1);
  }

  const python = findPython();
  const child = spawn(python.command, [SERVER, ...process.argv.slice(2)], {
    stdio: "inherit",
  });

  // Let the Python process own Ctrl+C so it can shut the HTTP server down.
  const forward = (signal) => () => {
    if (!child.killed) child.kill(signal);
  };
  process.on("SIGINT", forward("SIGINT"));
  process.on("SIGTERM", forward("SIGTERM"));

  child.on("error", (error) => {
    console.error(`agent-signals: failed to start ${python.command}: ${error.message}`);
    process.exit(1);
  });
  child.on("exit", (code, signal) => {
    process.exit(signal ? 1 : code === null ? 1 : code);
  });
}

main();
