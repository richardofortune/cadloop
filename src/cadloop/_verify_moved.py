"""Where `cadloop-verify` went, and why.

The check behind that command was one model's involute gear geometry. It ran
entirely off constants compiled into the package, so from an install with no
model anywhere on disk it printed a full green report about a spirograph the
caller had never seen. A command that looks like it verified your work and did
not is worse than no command, so it moved out of the distribution and now sits
beside the model it is about.

This stub exists only so 0.2.0 users meet a reason instead of `command not
found`. It goes away in 0.4.0.
"""

from __future__ import annotations

import sys

MESSAGE = """cadloop-verify has been removed.

It checked one model: the spirograph worked example, its involute gears and
its sheet layout. Those checks now live in the repository, beside the model,
rather than inside the package:

    git clone https://github.com/richardofortune/cadloop
    cd cadloop
    pip install -e ".[verify]"
    python models/verify_spirograph.py

Why it moved: the gear half ran off constants baked into the package, so on an
install without the model it reported "14/14 parts mesh cleanly" about parts
you do not have. cadloop itself cannot tell you whether your part works. That
step is yours to write, and it belongs next to your model.

This message is here for anyone upgrading from 0.2.0 and will be removed in
0.4.0."""


def main() -> int:
    # stderr and a non-zero exit, so a Makefile or CI step that called this
    # stops rather than carrying on as though the check had passed.
    print(MESSAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
