"""
Runs every check in this folder and prints one line per suite.

    python -m tests.checks.run_all           # everything
    python -m tests.checks.run_all leads     # only suites whose name matches

These are end-to-end checks against the real app with throwaway databases, not
unit tests: each one signs in, drives pages, and cleans up after itself. They
live in the repo because the first set was written in a temporary folder and
was deleted by the system a week later.
"""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))


def suites(pattern: str = "") -> list[str]:
    names = sorted(f for f in os.listdir(HERE)
                   if f.startswith("check_") and f.endswith(".py"))
    return [n for n in names if pattern.lower() in n.lower()]


def main(argv: list[str]) -> int:
    pattern = argv[0] if argv else ""
    chosen = suites(pattern)
    if not chosen:
        print(f"No checks match {pattern!r}. Available: {', '.join(suites())}")
        return 1

    width = max(len(n) for n in chosen)
    failed = []
    for name in chosen:
        started = time.perf_counter()
        # utf-8 explicitly: the checks print quotes and arrows, and Windows'
        # default codepage cannot decode them -- which crashed this runner
        # rather than the suite it was reporting on.
        result = subprocess.run([sys.executable, os.path.join(HERE, name)],
                                capture_output=True, text=True, cwd=ROOT,
                                encoding="utf-8", errors="replace")
        took = time.perf_counter() - started
        output = (result.stdout or "") + (result.stderr or "")
        summary = next((line.strip() for line in reversed(output.splitlines())
                        if line.strip().startswith("ALL ")), "")
        if result.returncode == 0:
            print(f"  PASS  {name:{width}}  {took:5.1f}s  {summary}")
        else:
            failed.append((name, output))
            first_fail = next((line.strip() for line in output.splitlines()
                               if line.strip().startswith("FAIL")), "")
            print(f"  FAIL  {name:{width}}  {took:5.1f}s  {first_fail}")

    print()
    if failed:
        for name, output in failed:
            print(f"--- {name} ---")
            print("\n".join(output.splitlines()[-25:]))
            print()
        print(f"{len(failed)} of {len(chosen)} suite(s) failed.")
        return 1
    print(f"All {len(chosen)} suite(s) passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
