"""Gathering unit entries from everywhere they might already exist.

Three sources, merged newest-first:

* the table tracked in git -- fetched from the remote when the network and the
  repository allow it, and read from the working tree when they do not;
* ``fits.json`` files exported under the data roots, which carry the same
  coefficients a fit would recompute;
* whatever datasets are open in the GUI right now.

Nothing here re-reads a raw frame. Collecting is meant to be cheap enough to
run when the window opens, so a unit measured on another machine, or exported
months ago and forgotten, is in the table without anyone remembering to add it.
"""

import json
import logging
import os
import subprocess

from fifa_gui.core.unittable import SCHEMA, UnitEntry, guess_unit_name

logger = logging.getLogger(__name__)

#: git is not allowed to sit there waiting for a credential prompt.
GIT_TIMEOUT_S = 20
GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}

#: Exported fit files are small; a deep scan of a data tree is not free.
SCAN_MAX_DEPTH = 4
FIT_FILENAMES = ("fits.json",)


def _git(args, cwd, timeout=GIT_TIMEOUT_S):
    env = dict(os.environ)
    env.update(GIT_ENV)
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, timeout=timeout,
        capture_output=True, text=True,
    )


def repo_root(start):
    """The work tree containing `start`, or None when it is not in one."""
    try:
        out = _git(["rev-parse", "--show-toplevel"], cwd=start, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def fetch_table_json(table_path, remote="origin"):
    """The committed table as text, preferring the remote's copy.

    Returns (text, where) with `where` one of "remote", "working tree" or
    "missing", so the caller can say which it got. Any git failure -- no
    network, no remote, not a repository, a credential prompt -- falls back to
    the file on disk rather than raising: collecting units must never be the
    thing that stops the window opening.
    """
    table_path = str(table_path)
    root = repo_root(os.path.dirname(os.path.abspath(table_path)))

    if root:
        rel = os.path.relpath(os.path.abspath(table_path), root).replace("\\", "/")
        try:
            head = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=root, timeout=10)
            branch = head.stdout.strip() if head.returncode == 0 else "main"
            fetched = _git(["fetch", "--quiet", remote, branch], cwd=root)
            if fetched.returncode == 0:
                shown = _git(["show", f"{remote}/{branch}:{rel}"], cwd=root)
                if shown.returncode == 0 and shown.stdout.strip():
                    return shown.stdout, "remote"
            else:
                logger.info("git fetch failed, using the working tree: %s",
                            fetched.stderr.strip()[:200])
        except (OSError, subprocess.SubprocessError) as exc:
            logger.info("git unavailable, using the working tree: %s", exc)

    if os.path.exists(table_path):
        with open(table_path, encoding="utf-8") as fh:
            return fh.read(), "working tree"
    return "", "missing"


def entries_from_table_json(text):
    """Unit entries from the JSON text of a saved table."""
    if not text.strip():
        return []
    payload = json.loads(text)
    if payload.get("schema") != SCHEMA:
        raise ValueError(f"not a unit table (schema {SCHEMA})")
    return [UnitEntry.from_dict(name, d)
            for name, d in payload.get("units", {}).items()]


def entries_from_fit_json(path):
    """Unit entries from one exported fits.json.

    A dataset that failed to fit has no coefficients and is skipped rather
    than becoming an entry full of nulls.
    """
    with open(path, encoding="utf-8") as fh:
        payload = json.load(fh)

    out = []
    for record in payload.get("datasets", []):
        if "coef_x" not in record or "coef_y" not in record:
            continue
        directory = record.get("directory", "")
        name = guess_unit_name(directory or record.get("dataset", ""))
        if not name:
            continue
        out.append(UnitEntry(
            name=name,
            coef_x=tuple(record["coef_x"]),
            coef_y=tuple(record["coef_y"]),
            sigma_x=_diag_sigma(record.get("cov_x")),
            sigma_y=_diag_sigma(record.get("cov_y")),
            rms_px=float(record.get("rms_residual_px", float("nan"))),
            n_frames=int(record.get("n_included", 0)),
            source=directory,
            updated_utc=record.get("generated_utc", ""),
        ))
    return out


def _diag_sigma(cov):
    if not cov:
        return (0.0, 0.0)
    try:
        return (abs(float(cov[0][0])) ** 0.5, abs(float(cov[1][1])) ** 0.5)
    except (IndexError, TypeError, ValueError):
        return (0.0, 0.0)


def scan_for_fit_exports(roots, max_depth=SCAN_MAX_DEPTH):
    """Every entry in every fits.json under `roots`, deduplicated by path."""
    seen, out = set(), []
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        root = os.path.abspath(root)
        base_depth = root.rstrip(os.sep).count(os.sep)
        for dirpath, dirnames, filenames in os.walk(root):
            if dirpath.count(os.sep) - base_depth >= max_depth:
                dirnames[:] = []
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for name in filenames:
                if name not in FIT_FILENAMES:
                    continue
                path = os.path.join(dirpath, name)
                if path in seen:
                    continue
                seen.add(path)
                try:
                    out += entries_from_fit_json(path)
                except (OSError, ValueError, KeyError) as exc:
                    logger.info("skipping %s: %s", path, exc)
    return out


def merge(table, entries):
    """Fold `entries` into `table`, newest `updated_utc` winning.

    An entry already in the table keeps its target: the stage or column
    someone chose for that unit is a decision, not a measurement, and must not
    be dropped because a fresher fit arrived.
    """
    added = updated = 0
    for entry in entries:
        existing = table.units.get(entry.name)
        if existing is not None:
            if (existing.updated_utc or "") >= (entry.updated_utc or ""):
                continue
            if existing.has_target:
                entry.target_from = existing.target_from
                if existing.target_from == "x":
                    try:
                        entry.set_target_x(existing.target_x)
                    except ValueError:
                        entry.clear_target()
                else:
                    entry.set_target_stage(existing.target_stage)
            updated += 1
        else:
            added += 1
        table.update(entry)
    return added, updated
