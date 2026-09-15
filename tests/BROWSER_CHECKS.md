Run the gameplay suite with `python -m unittest discover -s tests -q`.

For browser checks, install the optional test dependencies:

```
python -m pip install playwright
python -m playwright install chromium
```

Start `python tests/visual_server.py` in one terminal, then run
`python tests/visual_browser.py` in another. The fixture server binds only to
127.0.0.1:5010; its setup routes are never registered by the production app.

The browser checks cover:

- Home, every tutorial step, and lobby preset changes.
- All 45 combinations of 2–6 players and 2–4 rows/columns on the wooden table.
- Projected card targeting, keyboard controls, drawing/swapping, and one burn per press.
- Own/opponent peeks, Jack/Queen switching, and Black King inspection/switching.
- Round results, chat, keyboard guide focus, and reduced motion.
- Duplicate snapshots during a flight: the flight continues and card nodes survive.
- Portrait, landscape, and short desktop windows; a real touchscreen tap in Chromium.
- Vibrant card rendering and state-only highlights, without outlining every burnable card.

Screenshots are saved under `artifacts/` for visual review. These are local Chromium
checks; they do not measure real-device frame rates or simulate internet latency.

## UI restoration

The layout and presentation were restored from `447ba08` (July 20), before the
September redesign. The pre-restoration commit is preserved at
`backup/before-ui-restoration-20260914`. Server burn fixes and deployment settings
remain intact.

The restored UI uses immediate state updates, cancellable cosmetic flights,
scale-aware card motion, retained board DOM nodes, and one layout/hover pass per
animation frame. Scores have reserved space, held cards match board scale, and
burn feedback does not dim the table. The obsolete redesign stylesheet was removed.
