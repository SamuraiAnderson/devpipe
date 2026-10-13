"""Measure WSL background launch behavior without leaving long-lived jobs.

Run: python examples/wsl_session/background_probe.py [--distribution Debian]
All jobs end after 0.4 seconds. Temporary markers are removed on exit.
The report describes this machine/version; it is not a survival guarantee.
"""
import argparse
import json
import platform
import shlex
import time

import redpymake as rpm


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--distribution", default=None)
    args = parser.parse_args()
    with rpm.local() as local, rpm.wsl(args.distribution) as linux:
        report = {
            "windows": platform.version(),
            "wsl": local.run("wsl.exe", "--version", env={"WSL_UTF8": "1"}, timeout=10).stdout,
            "distribution": linux.run("cat", "/etc/os-release", timeout=20).stdout,
            "kernel": linux.run("uname", "-r", timeout=10).stdout,
            "shell": linux.run("sh", "-c", 'readlink /proc/$$/exe', timeout=10).stdout,
            "cases": [],
        }
        directory = linux.run("mktemp", "-d", "/tmp/redpymake-wsl-probe.XXXXXXXX", timeout=10).stdout.strip()
        if not directory.startswith("/tmp/redpymake-wsl-probe.") or "/" in directory[len("/tmp/"):]:
            raise ValueError(f"unexpected temporary path: {directory!r}")
        work = linux.at(directory)
        names = ["explicit_wait", "inherited_pipes", "nohup_redirected", "nohup_with_sleep"]
        if linux.run("sh", "-c", "command -v setsid", check=False, timeout=10).ok:
            names.append("setsid_redirected")
        try:
            for name in names:
                child = shlex.quote(f"printf started > {name}.started; sleep 0.4; printf done > {name}.done")
                commands = {
                    "explicit_wait": f'sh -c {child} & wait "$!"',
                    "inherited_pipes": f"sh -c {child} &",
                    "nohup_redirected": f"nohup sh -c {child} </dev/null >/dev/null 2>&1 &",
                    "nohup_with_sleep": f"nohup sh -c {child} </dev/null >/dev/null 2>&1 & sleep 1",
                    "setsid_redirected": f"setsid sh -c {child} </dev/null >/dev/null 2>&1 &",
                }
                result = work.run(commands[name], shell=True, check=False, timeout=10)
                # Deliberately no WSL process during this observation interval.
                time.sleep(0.8)
                report["cases"].append({
                    "name": name, "launcher_rc": result.rc, "stderr": result.stderr,
                    "run_seconds": round(result.duration, 3),
                    "started": work.path(name + ".started").exists(),
                    "completed": work.path(name + ".done").exists(),
                })
        finally:
            # Only known marker names in the unique directory; no process-wide kill.
            work.run("rm", "-f", "--", *(name + suffix for name in names
                                         for suffix in (".started", ".done")), timeout=10)
            linux.run("rmdir", "--", directory, timeout=10)
        print(json.dumps(report, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
