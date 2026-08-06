#!/usr/bin/env python3
"""CLI entrypoint.

  python main.py                 run the Tuesday night shift simulation
  python main.py --save PATH     also persist the shift state to PATH (JSON)
  python main.py --load PATH     print a previously-saved shift's report instead of simulating
  python main.py --quiet         suppress the live narrative, print only the final report
"""
from __future__ import annotations

import argparse
import json
import sys

from fleet_orchestrator import persistence
from fleet_orchestrator.scenario import run_tuesday_night


def main():
    parser = argparse.ArgumentParser(description="Multi-OEM fleet orchestration simulation")
    parser.add_argument("--save", metavar="PATH", help="persist shift state to this JSON file")
    parser.add_argument("--load", metavar="PATH", help="load and print a previously saved shift state")
    parser.add_argument("--quiet", action="store_true", help="only print the final shift report + dashboard")
    args = parser.parse_args()

    if args.load:
        data = persistence.load_shift_state(args.load)
        print(json.dumps(data, indent=2))
        return 0

    dispatcher, monitor, narrative = run_tuesday_night(verbose=not args.quiet)

    if args.quiet:
        print(monitor.shift_report(dispatcher.controllers, dispatcher.zones))

    print("\n" + monitor.dashboard(dispatcher.controllers))

    if args.save:
        persistence.save_shift_state(args.save, day="Tue", monitor=monitor,
                                      controllers=dispatcher.controllers,
                                      schedule={rid: c.tasks for rid, c in dispatcher.controllers.items()})
        print(f"\nShift state saved to {args.save}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
