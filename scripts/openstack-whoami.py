#!/usr/bin/env python3
"""Print which OpenStack PROJECT each clouds.yaml profile authenticates into.

An application credential is bound to the project its creator was scoped to when
it was made, permanently — you cannot re-scope one. That binding lives in
Keystone, not in clouds.yaml, so an app-credential profile carries no
project_name/project_id field and the file cannot tell you where it lands. Two
profiles with different names can be the same project.

That matters because husk's isolation story rests on it: pools sharing a name are
kept apart ONLY by being in different projects (slot ownership is the husk-pool
metadata tag, and huskd sees only servers in the project it authenticated into).
This makes the boundary visible instead of assumed.

Prints nothing secret — profile name, auth type, project id/name.
"""

from __future__ import annotations

import sys


def main(argv: list[str]) -> int:
    import openstack
    import openstack.config

    try:
        regions = openstack.config.OpenStackConfig().get_all()
    except Exception as e:
        print(f"could not read any clouds.yaml: {e}", file=sys.stderr)
        return 2

    wanted = set(argv[1:])
    names = [r.name for r in regions]
    if wanted:
        names = [n for n in names if n in wanted]
        missing = wanted - set(names)
        for m in sorted(missing):
            print(f"{m:<20} NOT FOUND in clouds.yaml", file=sys.stderr)
    if not names:
        print("no matching cloud profiles", file=sys.stderr)
        return 2

    rows: list[tuple[str, str, str]] = []
    for name in names:
        try:
            conn = openstack.connect(cloud=name)
            # current_project_id comes from the issued token, so it reflects what
            # the credential ACTUALLY grants — not what the file claims.
            pid = conn.current_project_id or "?"
            try:
                pname = conn.identity.get_project(pid).name
            except Exception:
                pname = "-"  # reading the project needs identity rights; id is enough
            auth_type = conn.config.config.get("auth_type", "?")
            rows.append((name, auth_type, f"{pid}  {pname}"))
        except Exception as e:
            rows.append((name, "-", f"AUTH FAILED: {type(e).__name__}: {e}"))

    w = max(len(r[0]) for r in rows)
    print(f"{'PROFILE':<{w}}  {'AUTH':<24}  PROJECT")
    for name, auth, proj in rows:
        print(f"{name:<{w}}  {auth:<24}  {proj}")

    # The whole point: say plainly whether the profiles are actually distinct.
    projects = [r[2].split()[0] for r in rows if not r[2].startswith("AUTH FAILED")]
    if len(projects) > 1 and len(set(projects)) == 1:
        print(
            "\nWARNING: every profile resolves to the SAME project. Pools sharing a"
            "\nname are NOT isolated — they will claim each other's slots.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
