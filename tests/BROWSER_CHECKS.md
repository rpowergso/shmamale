Run the gameplay suite with `python -m unittest discover -s tests -q`.

For browser checks, install the optional test dependencies:

```
python -m pip install playwright
python -m playwright install chromium
```

Start `python tests/visual_server.py` in one terminal, then run
`python tests/visual_browser.py` in another. The fixture server binds only to
127.0.0.1:5010; its setup routes are never registered by the production app.

The browser checks cover six-player square and rectangular grids, consistent
card dimensions, held-card separation, keyboard targeting, drawing and swapping,
immediate burn feedback, one attempt per press, retained misses, round results,
and narrow viewports. Screenshots are saved under `artifacts/` for visual review.
