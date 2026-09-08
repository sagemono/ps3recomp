#!/usr/bin/env python3
"""Port regression gate.

Runs each registered port, captures its milestone log (see
include/ps3emu/milestone.h) and diffs it against a checked-in golden. This is
what makes it safe to change the shared runtime: the titles are not all being
actively worked on, so without this nothing tells you that a fix for one game
silently broke another.

    python tools/regress.py list
    python tools/regress.py record vf5        # capture a new golden
    python tools/regress.py check vf5         # diff against it
    python tools/regress.py check --all       # every registered port

Ports are registered in tools/regress_ports.toml; goldens live beside it in
tools/regress/. Game binaries are never in the repo, so a port whose directory
or executable is missing is reported as SKIPPED rather than failing -- the gate
has to be usable by someone who owns two of the seven titles.

What counts as a failure:

  LOST     a key the golden has and this run does not. A call the title used to
           make and no longer does. Always a failure -- except for an
           hle:unresolved:* key, whose disappearance means the import gained a
           handler; that is reported as RESOLVED and passes.
  UNRESOLVED  a firmware import that has BECOME unresolved. Always a failure,
           even though it is a "new" key. The mirror image of RESOLVED.
  DROPPED  an occurrence count that fell by an order of magnitude, or a scalar
           that went down. Rendering stopping looks like this.

  NEW / RAISED / ORDER are reported but do not fail: they are as likely to be
  progress as regression, and a gate that cries wolf gets ignored.

A port's `expect` says what finishing looks like: "timeout" (it reached a frame
loop and never exits -- the usual case), "exit0" (a clean exit), or "any" (the
terminal state is not stable enough to assert; compare the milestones only).
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile

from msvc_env import environ as toolchain_environ

try:
    import tomllib
except ModuleNotFoundError:                      # Python < 3.11
    try:
        import tomli as tomllib
    except ModuleNotFoundError:
        tomllib = None

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REGISTRY = os.path.join(ROOT, "tools", "regress_ports.toml")
GOLDEN_DIR = os.path.join(ROOT, "tools", "regress")

# Boot is serial for its first stretch, so ordering is meaningful there and pure
# thread-scheduling noise after it. Only compare order inside that prefix.
ORDER_PREFIX = 25

# A count is only "dropped" if it fell by a whole bucket-decade or more; see the
# bucket rationale in runtime/milestone.c.
BUCKETS = ["0", "1", "2-9", "10-99", "100-999", "1k-9k", "10k-99k", "100k+"]


# --------------------------------------------------------------------------
# registry + log parsing
# --------------------------------------------------------------------------

def build_toolkit(spec):
    """Build the toolkit itself before testing any port against it.

    Without this the gate happily runs ports against whatever
    ps3recomp_runtime.lib happened to be lying around, which is exactly the
    false green it exists to prevent.
    """
    cmd = spec.get("build")
    if not cmd:
        return True, ""
    cp = subprocess.run(expand(cmd), cwd=ROOT, shell=True,
                        env=gate_env(), capture_output=True, text=True)
    if cp.returncode != 0:
        tail = (cp.stdout + cp.stderr).strip().splitlines()[-15:]
        return False, "\n    ".join(tail)
    return True, ""


def build_vars():
    """Toolchain placeholders for the registry's build commands.

    Windows needs clang-cl specifically: the runtime uses __atomic_* builtins,
    __int128 and __builtin_bswap*, none of which cl has. Everywhere else the
    system compiler is fine, and Ninja is preferred but not assumed -- a stock
    Debian has cmake without it.
    """
    if os.name == "nt":
        return {"cc": "clang-cl", "cxx": "clang-cl", "generator": "Ninja",
                "runtime_lib": "ps3recomp_runtime.lib"}
    cc = os.environ.get("CC") or shutil.which("clang") or shutil.which("gcc") or "cc"
    cxx = os.environ.get("CXX") or shutil.which("clang++") or shutil.which("g++") or "c++"
    return {"cc": cc, "cxx": cxx,
            "generator": "Ninja" if shutil.which("ninja") else "Unix Makefiles",
            "runtime_lib": "libps3recomp_runtime.a"}


def expand(cmd):
    """Fill the registry's placeholders.

    Registry commands run through the platform shell, and $VAR / %VAR% are
    spelled differently in sh and cmd, so neither works on both. Explicit
    placeholders do.
    """
    out = cmd.replace("{toolkit}", ROOT.replace("\\", "/"))
    for k, v in build_vars().items():
        out = out.replace("{%s}" % k, v)
    return out


def gate_env():
    """Environment for build and run steps.

    On Windows this is where the MSVC toolchain comes from: clang-cl needs the
    headers and import libraries vcvars sets up, and nothing outside CI does
    that for us. PS3RECOMP_DIR tells a port which toolkit it is being built
    against -- without it a port rebuilds against whatever sibling checkout it
    normally points at, and the gate tests a different tree than the one being
    changed.
    """
    env = toolchain_environ()
    env["PS3RECOMP_DIR"] = ROOT
    return env


def load_registry():
    if tomllib is None:
        sys.exit("regress.py needs Python 3.11+ (tomllib) or `pip install tomli`")
    if not os.path.exists(REGISTRY):
        sys.exit("no port registry at %s" % REGISTRY)
    with open(REGISTRY, "rb") as f:
        data = tomllib.load(f)
    toolkit = data.get("toolkit", {})
    ports = {}
    for p in data.get("port", []):
        # Every port so far takes the same two CMake cache variables and keeps
        # its generated NID table in the same place, so the recipe lives once
        # in [toolkit].port_build. A port sets its own `build` only if it
        # genuinely differs.
        p.setdefault("build", toolkit.get("port_build"))
        ports[p["name"]] = p
    return ports, toolkit


def parse_log(path):
    """-> (ordered [keys], {key: bucket}, {key: int})"""
    stream, counts, kv = [], {}, {}
    if not os.path.exists(path):
        return stream, counts, kv
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if parts[0] == "count" and len(parts) == 3:
                counts[parts[1]] = parts[2]
            elif parts[0] == "kv" and len(parts) == 3:
                try:
                    kv[parts[1]] = int(parts[2])
                except ValueError:
                    pass
            elif len(parts) == 2 and parts[0].isdigit():
                stream.append(parts[1])
    return stream, counts, kv


def golden_path(name):
    return os.path.join(GOLDEN_DIR, name + ".txt")


# --------------------------------------------------------------------------
# running a port
# --------------------------------------------------------------------------

def run_port(port, build=True):
    """Run one port and return (status, log_path, note).

    status is 'ok' when the run matched the port's `expect`, 'skip' when the
    port is not present on this machine, or 'error' otherwise.
    """
    name = port["name"]
    pdir = os.path.normpath(os.path.join(ROOT, port["dir"]))
    if not os.path.isdir(pdir):
        return "skip", None, "no directory at %s" % pdir

    # The gate tells the port where the toolkit under test lives; the port's
    # build command uses it. Without that the port would happily rebuild against
    # whatever sibling checkout it normally points at, and the gate would be
    # testing a different tree than the one being changed.
    penv = gate_env()
    penv.update({str(k): str(v) for k, v in port.get("env", {}).items()})

    if build and port.get("build"):
        cp = subprocess.run(expand(port["build"]), cwd=pdir, shell=True,
                            env=penv, capture_output=True, text=True)
        if cp.returncode != 0:
            tail = (cp.stdout + cp.stderr).strip().splitlines()[-15:]
            return "error", None, "build failed:\n    " + "\n    ".join(tail)

    cmd = list(port["cmd"])
    # Resolve the executable against the PORT directory, not ours. Python looks a
    # relative argv[0] up against the parent process cwd even when cwd= is set,
    # so a bare "build-gate/vf5.exe" is simply not found.
    exe = os.path.join(pdir, cmd[0])
    for cand in (exe, exe + ".exe"):
        if os.path.exists(cand):
            cmd[0] = os.path.abspath(cand)
            break
    else:
        if not shutil.which(cmd[0]):
            return "skip", None, "no executable at %s (build it first)" % cmd[0]

    log = os.path.join(tempfile.gettempdir(), "ps3recomp_regress_%s.txt" % name)
    if os.path.exists(log):
        os.remove(log)

    env = dict(penv)
    env["PS3_MILESTONE_OUT"] = log
    # Verbose logging starves guest threads and changes what the title does, so
    # a gate run with it on would be measuring a different program.
    env.setdefault("PS3_VERBOSE", "0")

    expect = port.get("expect", "timeout")
    timeout = float(port.get("timeout", 60))
    timed_out = False
    rc = None
    try:
        cp = subprocess.run(cmd, cwd=pdir, env=env, timeout=timeout,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        rc = cp.returncode
    except subprocess.TimeoutExpired:
        timed_out = True
    except OSError as e:
        return "error", None, "could not launch: %s" % e

    # A title that boots into its frame loop never exits, so being killed on the
    # timeout is the SUCCESS case for most ports. The milestone stream is written
    # as each key is first seen precisely so that a kill still leaves a record.
    if expect == "any":
        # For a port whose TERMINAL state is unstable but whose run is not. Tokyo
        # Jungle tears down cleanly and then sometimes exits 0 and sometimes dies
        # with an access violation, after every milestone worth comparing has
        # already been recorded. Gating on the exit code there would just be a
        # coin flip; gating on what it did still works.
        pass
    elif expect == "timeout":
        if not timed_out:
            return "error", log, "exited early (rc=%s); expected to still be running" % rc
    elif expect == "exit0":
        if timed_out:
            return "error", log, "timed out after %gs; expected a clean exit" % timeout
        if rc != 0:
            return "error", log, "exit code %s; expected 0" % rc

    if not os.path.exists(log):
        return "error", None, ("no milestone log produced -- is this port built "
                               "against a runtime that has runtime/milestone.c?")
    return "ok", log, ""


# --------------------------------------------------------------------------
# diffing
# --------------------------------------------------------------------------

def bucket_index(b):
    return BUCKETS.index(b) if b in BUCKETS else -1


def diff(golden, current):
    """-> (failures, notes), each a list of strings."""
    g_stream, g_counts, g_kv = golden
    c_stream, c_counts, c_kv = current
    g_set, c_set = set(g_stream), set(c_stream)
    fails, notes = [], []

    for k in g_stream:
        if k not in c_set:
            if k.startswith("hle:unresolved:"):
                # The import RESOLVED. Losing an unresolved-NID key is the good
                # direction and must not read as a regression, or every genuine
                # improvement to the HLE surface fails the gate.
                notes.append("RESOLVED    %s  (import now has a handler)" % k)
            else:
                fails.append("LOST        %s" % k)

    for k in c_stream:
        if k not in g_set:
            if k.startswith("hle:unresolved:"):
                fails.append("UNRESOLVED  %s  (import stopped resolving)" % k)
            else:
                notes.append("NEW         %s" % k)

    for k in sorted(g_set & c_set):
        gi, ci = bucket_index(g_counts.get(k, "")), bucket_index(c_counts.get(k, ""))
        if gi < 0 or ci < 0 or gi == ci:
            continue
        if ci < gi:
            fails.append("DROPPED     %s  %s -> %s" % (k, g_counts[k], c_counts[k]))
        else:
            notes.append("RAISED      %s  %s -> %s" % (k, g_counts[k], c_counts[k]))

    for k in sorted(set(g_kv) | set(c_kv)):
        gv, cv = g_kv.get(k), c_kv.get(k)
        if gv == cv:
            continue
        if gv is None:
            notes.append("NEW         %s = %d" % (k, cv))
        elif cv is None:
            fails.append("LOST        %s (was %d)" % (k, gv))
        elif cv < gv:
            fails.append("DROPPED     %s  %d -> %d" % (k, gv, cv))
        else:
            notes.append("RAISED      %s  %d -> %d" % (k, gv, cv))

    # Order, but only across the serial part of boot -- past that the sequence is
    # thread scheduling and any comparison is noise.
    common = g_set & c_set
    g_head = [k for k in g_stream[:ORDER_PREFIX] if k in common]
    c_head = [k for k in c_stream if k in common][:len(g_head)]
    if g_head != c_head:
        for i, (a, b) in enumerate(zip(g_head, c_head)):
            if a != b:
                notes.append("ORDER       at #%d: expected %s, got %s" % (i + 1, a, b))
                break

    return fails, notes


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_list(ports, args):
    for name in sorted(ports):
        p = ports[name]
        g = "golden" if os.path.exists(golden_path(name)) else "NO GOLDEN"
        here = "" if os.path.isdir(os.path.normpath(os.path.join(ROOT, p["dir"]))) else "  (not on this machine)"
        print("  %-16s %-10s %s%s" % (name, g, p["dir"], here))
    return 0


def cmd_record(ports, args):
    rc = 0
    for name in args.names:
        port = ports[name]
        print("[%s] running..." % name)
        status, log, note = run_port(port, build=not args.no_build)
        if status != "ok":
            print("  %s: %s" % (status.upper(), note))
            rc = rc or (0 if status == "skip" else 2)
            continue
        os.makedirs(GOLDEN_DIR, exist_ok=True)
        shutil.copyfile(log, golden_path(name))
        stream, counts, kv = parse_log(log)
        print("  recorded %d keys, %d scalars -> %s"
              % (len(stream), len(kv), os.path.relpath(golden_path(name), ROOT)))
        for k in sorted(kv):
            print("    %-24s %d" % (k, kv[k]))
        unres = [k for k in stream if k.startswith("hle:unresolved:")]
        if unres:
            print("  note: %d unresolved imports are baked into this golden" % len(unres))
    return rc


def cmd_check(ports, args):
    names = sorted(ports) if args.all else args.names
    worst = 0
    for name in names:
        port = ports[name]
        gp = golden_path(name)
        if not os.path.exists(gp):
            print("[%s] NO GOLDEN -- run: python tools/regress.py record %s" % (name, name))
            continue
        status, log, note = run_port(port, build=not args.no_build)
        if status == "skip":
            print("[%s] SKIPPED  (%s)" % (name, note))
            continue
        if status == "error":
            print("[%s] ERROR    %s" % (name, note))
            worst = 2
            continue

        fails, notes = diff(parse_log(gp), parse_log(log))
        if fails:
            print("[%s] FAIL     %d regression(s)" % (name, len(fails)))
            for line in fails[:40]:
                print("    " + line)
            if len(fails) > 40:
                print("    ... and %d more" % (len(fails) - 40))
            worst = max(worst, 1)
        else:
            print("[%s] PASS" % name)
        if notes and (args.verbose or fails):
            for line in notes[:20]:
                print("    " + line)
            if len(notes) > 20:
                print("    ... and %d more informational" % (len(notes) - 20))
        elif notes:
            print("    (%d informational: NEW/RAISED/ORDER -- pass -v to see them)"
                  % len(notes))
    return worst


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="show registered ports and golden status")

    r = sub.add_parser("record", help="run a port and save its golden")
    r.add_argument("names", nargs="+")
    r.add_argument("--no-build", action="store_true")

    c = sub.add_parser("check", help="run a port and diff against its golden")
    c.add_argument("names", nargs="*")
    c.add_argument("--all", action="store_true")
    c.add_argument("--no-build", action="store_true")
    c.add_argument("-v", "--verbose", action="store_true",
                   help="also show NEW / RAISED / ORDER on a passing port")

    args = ap.parse_args()
    ports, toolkit = load_registry()

    if args.cmd in ("record", "check"):
        unknown = [n for n in getattr(args, "names", []) if n not in ports]
        if unknown:
            sys.exit("unknown port(s): %s (see: regress.py list)" % ", ".join(unknown))
        if args.cmd == "check" and not args.names and not args.all:
            sys.exit("name a port, or pass --all")

    if args.cmd in ("record", "check") and not args.no_build:
        ok, note = build_toolkit(toolkit)
        if not ok:
            print("[toolkit] BUILD FAILED -- not running any port against a stale library:")
            print("    " + note)
            return 2

    return {"list": cmd_list, "record": cmd_record, "check": cmd_check}[args.cmd](ports, args)


if __name__ == "__main__":
    sys.exit(main())
