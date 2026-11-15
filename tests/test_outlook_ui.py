"""fetch_from_outlook: the Outlook window is never touched here; a fake runner stands in for the script."""

from __future__ import annotations

import pytest

from new_outlook_mcp import outlook_ui, tools
from new_outlook_mcp.sync import SyncResult


def _runner(reply: str, seen: list | None = None):
    def run(args, timeout):
        if seen is not None:
            seen.append((args, timeout))
        return reply
    return run


OK = "rows_first=12\nrows=40\nscroll_rounds=3\nopened=5\n"


def test_run_search_passes_clean_arguments_and_parses_the_reply():
    seen = []
    run = outlook_ui.run_search("  budget   report ", open_top=5, scroll_rounds=3, runner=_runner(OK, seen))
    assert (run.rows_first, run.rows, run.scroll_rounds, run.opened, run.note) == (12, 40, 3, 5, None)
    assert seen[0][0] == ["budget report", "3", "5"]


def test_limits_are_clamped():
    seen = []
    outlook_ui.run_search("x", open_top=10_000, scroll_rounds=10_000, runner=_runner(OK, seen))
    assert seen[0][0] == ["x", str(outlook_ui.MAX_SCROLL_ROUNDS), str(outlook_ui.MAX_OPEN)]


@pytest.mark.parametrize("bad", ["", "   ", "a\nb", "tab\there", "x" * 201, "bell\x07"])
def test_bad_queries_never_reach_outlook(bad):
    def forbidden(args, timeout):
        raise AssertionError("the script must not run")
    with pytest.raises(ValueError):
        outlook_ui.run_search(bad, runner=forbidden)


def test_script_errors_and_partial_runs_are_reported():
    with pytest.raises(outlook_ui.OutlookUIError, match="search box did not get the focus"):
        outlook_ui.run_search("x", runner=_runner("error=search box did not get the focus"))
    with pytest.raises(outlook_ui.OutlookUIError, match="unexpected reply"):
        outlook_ui.run_search("x", runner=_runner("something else"))
    run = outlook_ui.run_search("x", runner=_runner(
        "rows_first=3\nscroll=stopped, another app came to the front\nrows=3\nscroll_rounds=1\nopened=0\n"))
    assert run.note == "stopped, another app came to the front"


def test_script_only_sends_the_documented_events():
    s = outlook_ui.SEARCH_SCRIPT
    # Keys: Cmd-A and the query into the search field, then Return. Nothing else is typed or pressed.
    assert s.count("se.keystroke(") == 2 and s.count("se.keyCode(") == 1 and "se.keyCode(36)" in s
    assert ".click()" in s and s.count(".click()") == 1  # the Edit > Find > Outlook Items Search menu item
    for word in ("Archive", "Delete", "Mark as", "send", "AXPress", "perform", "actions"):
        assert word not in s, word
    # Every key press and wheel event is behind a front-app check.
    assert s.index("proc.frontmost()") < s.index("se.keystroke(")
    assert "if (!inFront())" in s


def test_fetch_tool_syncs_and_reports(loaded):
    calls = []

    def syncer(archive, names):
        calls.append(names)
        return [SyncResult(source="hxstore", status="ok", inserted=2, message="ok")]

    out = tools.fetch_from_outlook(loaded, "budget", open_top=5, runner=_runner(OK), syncer=syncer,
                                   sleep=lambda s: None)
    assert calls == [["hxstore"]]
    assert (out["result_rows"], out["opened"], out["query"]) == (40, 5, "budget")
    assert out["sync"][0]["status"] == "ok" and "warning" not in out and "freshness" in out


def test_fetch_tool_turns_failures_into_input_errors(loaded):
    def boom(args, timeout):
        raise outlook_ui.OutlookUIError("Outlook did not finish in time")

    def no_sync(archive, names):
        raise AssertionError("no sync after a failed run")

    with pytest.raises(tools.ToolInputError, match="Could not operate Outlook"):
        tools.fetch_from_outlook(loaded, "budget", runner=boom, syncer=no_sync, sleep=lambda s: None)
    with pytest.raises(tools.ToolInputError):
        tools.fetch_from_outlook(loaded, "", runner=boom, syncer=no_sync, sleep=lambda s: None)
