"""Make New Outlook fetch older mail by running a search in its own window.

New Outlook only downloads old mail when its window asks for it. There is no file,
scripting command or link that does this, so this module operates the window through
macOS UI scripting (System Events): it opens the search box, types the query, scrolls
the result list and selects the first results. The mail then lands in HxStore.hxd, where
the normal sync finds it. Measured on Outlook 16.115 (2026-11-15):

- a result that was only scrolled past arrives with subject, sender, date, folder,
  Message-ID and a short preview, but without body, recipients or attachment records;
- a result that was selected arrives complete, with its whole conversation.

What this touches, and what it never does:

- It takes over Outlook's window while it runs, switches it to the Mail view and types into its
  search box. Outlook is left in the Mail view, showing the search.
- Selecting a message marks it read if it was unread. Nothing else in the mailbox changes.
- It sends only these events: Cmd-A, the query and Return into the search field (checked to
  have the focus first), and scroll-wheel events over the result list, for which it moves the
  mouse pointer there and back. Each is sent only while Outlook is the front app. It never
  presses a row action such as Archive or Delete, never sends, moves or deletes.

Needs Accessibility permission for the program that runs it. The element paths belong to
one Outlook build; when they stop matching, the script reports which step failed.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

MAX_QUERY_CHARS = 200
MAX_OPEN = 100
MAX_SCROLL_ROUNDS = 60

#: JavaScript for Automation. argv: query, scroll rounds, results to open. Prints "key=value" lines.
SEARCH_SCRIPT = r"""
ObjC.import("CoreGraphics");

function run(argv) {
  const query = argv[0], scrollRounds = parseInt(argv[1], 10), openTop = parseInt(argv[2], 10);
  const outlook = Application("Microsoft Outlook");
  const se = Application("System Events");
  const proc = se.processes.byName("Microsoft Outlook");
  const out = [];
  const done = () => out.join("\n");

  // Keys and wheel events go to whatever app is in front, so each one is preceded by this check.
  function inFront() {
    if (proc.frontmost()) return true;
    outlook.activate(); delay(0.8);
    return proc.frontmost();
  }
  function mainWindow() {
    return proc.windows().find(w => {
      try { return w.attributes.byName("AXIdentifier").value() === "OutlookWindow"; } catch (e) { return false; }
    });
  }
  function listPane() { return mainWindow().splitterGroups[0].splitterGroups[0].splitterGroups[0].groups[0]; }
  // -1: the mail view is not there at all. 0: it is there, with no result list (a search without hits).
  function rowCount() {
    try { return listPane().tables[0].rows.length; } catch (e) {}
    try { mainWindow().splitterGroups[0].splitterGroups[0].splitterGroups[0].role(); return 0; } catch (e) { return -1; }
  }
  function waitStable(polls) {
    let last = -2, stable = 0;
    for (let i = 0; i < polls; i++) {
      delay(0.5);
      const n = rowCount();
      stable = (n === last) ? stable + 1 : 0;
      last = n;
      if (stable >= 4) break;
    }
    return last;
  }
  function focusedIsSearchField() {
    try { return proc.attributes.byName("AXFocusedUIElement").value().subrole() === "AXSearchField"; } catch (e) { return false; }
  }

  outlook.activate(); delay(1.0);
  // Outlook may be showing the calendar or another module: the search box there does not search mail.
  try {
    proc.menuBars[0].menuBarItems.byName("View").menus[0].menuItems.byName("Go To").menus[0]
        .menuItems.byName("Mail").click();
  } catch (e) { return "error=could not switch Outlook to its Mail view"; }
  delay(1.0);
  try {
    proc.menuBars[0].menuBarItems.byName("Edit").menus[0].menuItems.byName("Find").menus[0]
        .menuItems.byName("Outlook Items Search").click();
  } catch (e) { return "error=search menu item not found"; }
  delay(0.7);
  if (!focusedIsSearchField()) return "error=search box did not get the focus";
  if (!proc.frontmost()) return "error=another app came to the front; nothing was typed";
  se.keystroke("a", {using: "command down"});
  se.keystroke(query);
  delay(0.4);
  if (!proc.frontmost() || !focusedIsSearchField()) return "error=the search box lost the focus before the search started";
  se.keyCode(36);
  const first = waitStable(40);
  if (first < 0) return "error=Outlook's mail list was not found after the search";
  out.push("rows_first=" + first);

  // Scroll with wheel events over the list: this loads more results and selects nothing.
  let rows = first, rounds = 0;
  if (scrollRounds > 0 && first > 0) {
    try {
      const pane = listPane(), pos = pane.position(), size = pane.size();
      const was = $.CGEventGetLocation($.CGEventCreate(null));
      let idle = 0;
      while (rounds < scrollRounds && idle < 3) {
        if (!inFront()) { out.push("scroll=stopped, another app came to the front"); break; }
        $.CGWarpMouseCursorPosition({x: pos[0] + size[0] / 2, y: pos[1] + size[1] * 0.6});
        delay(0.1);
        for (let k = 0; k < 20; k++) {
          $.CGEventPost(0, $.CGEventCreateScrollWheelEvent(null, 1, 1, -15));
          delay(0.02);
        }
        rounds++;
        delay(1.2);
        const n = rowCount();
        idle = (n > rows) ? 0 : idle + 1;
        if (n > rows) rows = n;
      }
      $.CGWarpMouseCursorPosition(was);
    } catch (e) { out.push("scroll=stopped, Outlook's window went out of view"); }
  }
  out.push("rows=" + rows);
  out.push("scroll_rounds=" + rounds);

  // Open: select rows from the top through their accessibility attribute, one at a time. No keys.
  // Rows that will not select are headers.
  let opened = 0;
  if (openTop > 0) {
    try {
      if (!inFront()) {
        out.push("open=skipped, another app came to the front");
      } else {
        const rowsNow = listPane().tables[0].rows;
        const total = rowsNow.length;
        for (let i = 0; i < total && opened < openTop; i++) {
          const sel = rowsNow[i].attributes.byName("AXSelected");
          if (!sel.value()) {
            try { sel.value = true; } catch (e) { continue; }
            delay(1.2);
          }
          if (sel.value()) opened++;
        }
      }
    } catch (e) { out.push("open=stopped, Outlook's window went out of view"); }
  }
  out.push("opened=" + opened);
  return done();
}
"""


class OutlookUIError(RuntimeError):
    """Outlook's window could not be operated. The message says which step failed."""


@dataclass
class SearchRun:
    rows_first: int  # result rows after the search, before scrolling (headers included)
    rows: int        # result rows after scrolling
    scroll_rounds: int
    opened: int
    note: str | None = None


def _run_script(args: list[str], timeout: float) -> str:
    try:
        # "-" reads the script from stdin and ends option parsing, so a query starting with "-" is safe.
        proc = subprocess.run(["osascript", "-l", "JavaScript", "-", *args], input=SEARCH_SCRIPT, capture_output=True, text=True,
                              timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise OutlookUIError("Outlook did not finish in time; its window may still show the search") from exc
    except OSError as exc:
        raise OutlookUIError(f"cannot run osascript: {type(exc).__name__}") from exc
    if proc.returncode != 0:
        err = proc.stderr.strip()
        if "-1719" in err or "-25211" in err or "not allowed assistive access" in err:
            raise OutlookUIError(
                "macOS has not allowed this program to operate other apps, or Outlook's window was not found. "
                "Allow it in System Settings > Privacy & Security > Accessibility, and keep Outlook's main window open.")
        if "-1743" in err:
            raise OutlookUIError("macOS has not allowed this program to control System Events "
                                 "(Privacy & Security > Automation)")
        raise OutlookUIError("Outlook's window did not respond as expected (its layout may have changed "
                             "with an update)")
    return proc.stdout


def run_search(query: str, *, open_top: int = 25, scroll_rounds: int = 30, runner=None) -> SearchRun:
    """Search in Outlook's window, scroll the results and select the first `open_top` of them."""
    query = query or ""
    # A line break would be typed as Return, a tab would move the focus: refuse them before anything runs.
    if any(ord(c) < 32 or ord(c) == 127 for c in query):
        raise ValueError("query contains line breaks or other control characters")
    query = " ".join(query.split())
    if not query:
        raise ValueError("query is required")
    if len(query) > MAX_QUERY_CHARS:
        raise ValueError(f"query is longer than {MAX_QUERY_CHARS} characters")
    open_top = max(0, min(int(open_top), MAX_OPEN))
    scroll_rounds = max(0, min(int(scroll_rounds), MAX_SCROLL_ROUNDS))
    timeout = 60 + scroll_rounds * 6 + open_top * 3
    out = (runner or _run_script)([query, str(scroll_rounds), str(open_top)], timeout)
    fields = dict(line.split("=", 1) for line in out.strip().splitlines() if "=" in line)
    if "error" in fields:
        raise OutlookUIError(fields["error"] + " (Outlook's layout may have changed with an update)")
    try:
        return SearchRun(rows_first=int(fields["rows_first"]), rows=int(fields["rows"]),
                         scroll_rounds=int(fields["scroll_rounds"]), opened=int(fields["opened"]),
                         note="; ".join(v for v in (fields.get("scroll"), fields.get("open")) if v) or None)
    except (KeyError, ValueError) as exc:
        raise OutlookUIError("unexpected reply from the Outlook script") from exc
