from __future__ import annotations

import argparse
import json

from .config import load_config
from .engine import Stowarr
from .server import serve


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve")
    plan = sub.add_parser("plan")
    plan.add_argument("torrent_hash")
    reconcile = sub.add_parser("reconcile")
    reconcile.add_argument("torrent_hash")
    reconcile.add_argument("--confirmation-token", required=True)
    args = parser.parse_args()
    manager = Stowarr(load_config(args.config))
    if args.command == "serve":
        serve(manager)
    elif args.command == "plan":
        print(json.dumps(manager.plan(args.torrent_hash).json(), indent=2))
    else:
        manager.consume_confirmation(args.confirmation_token, "reconcile", args.torrent_hash, {"auxiliaryFiles": []})
        print(json.dumps(manager.reconcile(args.torrent_hash), indent=2))
