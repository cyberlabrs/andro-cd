#!/usr/bin/env python3
"""androcd CLI — validate manifests offline, diff/sync against a running server.

  python cli.py validate ./manifests
  python cli.py apps        --server http://localhost:8080
  python cli.py diff  NAME  --server http://localhost:8080
  python cli.py sync  NAME  --server http://localhost:8080

Auth (when the server runs with AUTH_MODE=github): pass a session cookie via
--session or the ANDROCD_SESSION env var (value of the 'androcd_session' cookie).
"""
import argparse
import glob
import json
import os
import sys
import urllib.error
import urllib.request

import yaml


def _api(args, method: str, path: str):
    req = urllib.request.Request(f"{args.server.rstrip('/')}{path}", method=method)
    session = args.session or os.getenv("ANDROCD_SESSION", "")
    if session:
        req.add_header("Cookie", f"androcd_session={session}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print(f"error {e.code}: {e.read().decode()[:300]}", file=sys.stderr)
        sys.exit(1)


def cmd_validate(args) -> None:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from app import templating
    from app.models import Manifest

    base = args.path if os.path.isdir(args.path) else os.path.dirname(args.path) or "."
    files = sorted(
        glob.glob(os.path.join(args.path, "**", "*.yaml"), recursive=True)
        + glob.glob(os.path.join(args.path, "**", "*.yml"), recursive=True)
    ) if os.path.isdir(args.path) else [args.path]

    # values.yaml files provide ${key} substitutions (same semantics as the server)
    values_by_dir: dict[str, dict] = {}
    manifest_files: list[str] = []
    for f in files:
        rel = os.path.relpath(f, base).replace(os.sep, "/")
        if templating.is_values_file(rel):
            try:
                data = yaml.safe_load(open(f))
                if isinstance(data, dict):
                    values_by_dir[os.path.dirname(rel).replace(os.sep, "/")] = templating.flatten(data)
            except yaml.YAMLError as e:
                print(f"✗ {f}: YAML parse error in values file: {e}")
            continue
        manifest_files.append(f)

    errors = 0
    count = 0
    for f in manifest_files:
        rel = os.path.relpath(f, base).replace(os.sep, "/")
        values = templating.values_for(rel, values_by_dir) if values_by_dir else {}
        try:
            docs = [d for d in yaml.safe_load_all(open(f)) if isinstance(d, dict)]
        except yaml.YAMLError as e:
            print(f"✗ {f}: YAML parse error: {e}")
            errors += 1
            continue
        for doc in docs:
            if values:
                doc = templating.substitute(doc, values)
            if doc.get("kind") == "ECSServiceSet":
                print(f"✓ {f}: ECSServiceSet '{doc.get('metadata', {}).get('name')}' "
                      f"({len((doc.get('spec') or {}).get('generators') or [])} generators)")
                count += 1
                continue
            try:
                m = Manifest.model_validate(doc)
                print(f"✓ {f}: {m.kind} '{m.name}' (cluster={m.spec.cluster}, wave={m.spec.wave})")
                count += 1
            except Exception as e:
                first = str(e).splitlines()
                print(f"✗ {f}: {doc.get('metadata', {}).get('name', '?')}: {first[1] if len(first) > 1 else first[0]}")
                errors += 1
    print(f"\n{count} valid, {errors} invalid")
    sys.exit(1 if errors else 0)


def cmd_apps(args) -> None:
    for a in _api(args, "GET", "/api/apps"):
        print(f"{a['name']:30} {a.get('kind', ''):18} {a['syncStatus']:10} {a['health']:12} {a.get('cluster') or ''}")


def cmd_diff(args) -> None:
    app = _api(args, "GET", f"/api/apps/{args.name}")
    print(f"{args.name}: {app['syncStatus']} / {app['health']}")
    for c in app.get("changes", []):
        print(f"  ~ {c}")
    if not app.get("changes"):
        print("  (in sync)")


def cmd_sync(args) -> None:
    result = _api(args, "POST", f"/api/apps/{args.name}/sync")
    print(f"{args.name}: {result['syncStatus']}")
    for a in result.get("lastActions", []):
        print(f"  ✓ {a}")


def main() -> None:
    p = argparse.ArgumentParser(prog="androcd")
    p.add_argument("--server", default=os.getenv("ANDROCD_SERVER", "http://localhost:8080"))
    p.add_argument("--session", default="", help="androcd_session cookie value")
    sub = p.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("validate", help="validate manifests offline")
    v.add_argument("path")
    v.set_defaults(fn=cmd_validate)

    sub.add_parser("apps", help="list apps").set_defaults(fn=cmd_apps)

    d = sub.add_parser("diff", help="show pending changes for an app")
    d.add_argument("name")
    d.set_defaults(fn=cmd_diff)

    s = sub.add_parser("sync", help="force sync an app")
    s.add_argument("name")
    s.set_defaults(fn=cmd_sync)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
