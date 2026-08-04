"""The removed command has to fail loudly, not quietly.

A caller upgrading from 0.2.0 has `cadloop-verify` in a Makefile or a CI step.
The two things that matter are that it stops the build rather than looking
like a pass, and that it says where the check went.
"""

from cadloop import _verify_moved


def test_exits_non_zero():
    assert _verify_moved.main() != 0


def test_names_where_the_check_went(capsys):
    _verify_moved.main()
    err = capsys.readouterr().err
    assert "models/verify_spirograph.py" in err
    assert "removed" in err.lower()
