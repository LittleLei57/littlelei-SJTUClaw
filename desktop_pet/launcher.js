"use strict";

// Electron's bundled CLI prints a long "exited with signal SIGINT" line on
// Windows. Launch the binary directly so intentional shutdown stays quiet and
// the PowerShell prompt is not visually disrupted.
const { spawn } = require("child_process");
const electron = require("electron");

let child = null;
let restartTimer = null;
let restartCount = 0;
let closing = false;

function startChild() {
  if (closing) return;
  child = spawn(electron, [__dirname], {
    cwd: __dirname,
    stdio: "inherit",
    windowsHide: false,
  });
  child.on("error", (error) => {
    console.error(`Desktop Pet failed to start: ${error.message}`);
  });
  child.on("close", (code, signal) => {
    child = null;
    const unexpected = !closing && (code !== 0 || (signal && !["SIGINT", "SIGTERM"].includes(signal)));
    if (unexpected) {
      const delay = Math.min(5000, 1000 * (2 ** restartCount));
      restartCount += 1;
      console.error(`Desktop Pet stopped unexpectedly; retrying in ${delay}ms.`);
      restartTimer = setTimeout(startChild, delay);
      return;
    }
    process.exitCode = code ?? (signal === "SIGINT" || signal === "SIGTERM" ? 0 : 1);
  });
}

function forward(signal) {
  if (closing) return;
  closing = true;
  if (restartTimer) clearTimeout(restartTimer);
  if (child && !child.killed) child.kill(signal);
}

process.on("SIGINT", () => forward("SIGINT"));
process.on("SIGTERM", () => forward("SIGTERM"));
startChild();
