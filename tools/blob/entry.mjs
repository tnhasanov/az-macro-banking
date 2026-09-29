/**
 * Was the module at `metaUrl` named on the command line, rather than imported?
 *
 * Compared as real paths. Building `file://` + argv[1] by hand only works on POSIX, and on Windows a
 * path is case-insensitive and the drive letter's case depends on who spelled it — so a comparison
 * of two spellings of one file can differ, and a script that decides it was merely imported runs
 * nothing and exits 0, which reads as success.
 */
import { realpathSync } from "node:fs";
import { fileURLToPath } from "node:url";

export function isEntryPoint(metaUrl) {
  if (!process.argv[1]) return false;
  let entry;
  try {
    entry = realpathSync(process.argv[1]);
  } catch {
    return false;
  }
  const self = fileURLToPath(metaUrl);
  return process.platform === "win32" ? entry.toLowerCase() === self.toLowerCase() : entry === self;
}
