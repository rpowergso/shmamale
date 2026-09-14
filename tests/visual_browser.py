from pathlib import Path
import json, time
from playwright.sync_api import sync_playwright

out = Path(__file__).resolve().parents[1] / 'artifacts'
out.mkdir(exist_ok=True)

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={'width': 1440, 'height': 1000}, device_scale_factor=1)
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.request.post('http://127.0.0.1:5010/qa/reset')
    page.goto('http://127.0.0.1:5010/multiplayer/VISUAL')
    page.locator('#username-input').fill('River')
    page.locator('#join-room-btn').click()
    page.locator('#ready-btn').wait_for()
    def setup(**data):
        response = page.request.post('http://127.0.0.1:5010/qa/setup', data=data)
        assert response.ok, response.text()
        page.locator('#game').wait_for(state='visible')
        page.wait_for_timeout(250)
        return response.json()['sid']
    for size in [4, 3, 2]:
        sid = setup(size=size, held=True)
        page.screenshot(path=str(out / f'grid-{size}-six-players.png'))
        if size > 2:
            widths = page.locator('.board-card').evaluate_all('(cards) => cards.map(c => c.getBoundingClientRect().width)')
            assert min(widths) >= 35, widths
            assert max(widths) - min(widths) < 1, widths
            overlaps = page.locator('.seat').evaluate_all('''seats => seats.flatMap(seat => {
                const cards = [...seat.querySelectorAll('.board-card, .held-card')];
                return cards.flatMap((card, i) => cards.slice(i+1).filter(other => {
                    const a = card.getBoundingClientRect(), b = other.getBoundingClientRect();
                    return a.left < b.right-1 && a.right > b.left+1 && a.top < b.bottom-1 && a.bottom > b.top+1;
                }).map(other => [card.dataset.index, other.dataset.index]));
            })''')
            assert not overlaps, overlaps
            assert page.locator('.board-card.keyboard-focus').count() == 0
    for rows, cols in [(2,4), (4,2), (3,4)]:
        setup(rows=rows, cols=cols)
        assert page.locator('.seat.me .board-card').count() == rows*cols
        widths = page.locator('.board-card').evaluate_all('(cards) => cards.map(c => c.getBoundingClientRect().width)')
        assert min(widths) >= 35
    sid = setup(size=3, my_turn=True)
    page.keyboard.press('2')
    page.keyboard.press('d')
    assert page.locator('.board-card.keyboard-focus').get_attribute('data-owner') == 'qa-1'
    assert page.locator('.board-card.keyboard-focus').get_attribute('data-index') == '1'
    page.locator('#draw-btn').click()
    page.locator('.seat.me .held-card').wait_for()
    page.locator(f'.board-card[data-owner="{sid}"][data-index="0"]').click()
    page.wait_for_timeout(230)
    assert page.locator('#discard-btn .rank').first.inner_text() == '7'
    assert page.locator('.board-card.keyboard-focus').count() == 0
    assert page.locator('.board-card.swap-mark').count() == 1
    assert page.locator('.board-card.swap-mark').get_attribute('data-owner') == sid
    assert page.locator('.board-card.swap-mark').get_attribute('data-index') == '0'
    sid = setup(size=4)
    # Wrong burn: pointer-down feedback, exactly one penalty, then a valid burn.
    target = page.locator(f'.board-card[data-owner="{sid}"][data-index="1"]')
    target.scroll_into_view_if_needed()
    box = target.bounding_box()
    page.mouse.move(box['x'] + box['width']/2, box['y'] + box['height']/2)
    before = time.perf_counter()
    page.mouse.down()
    assert page.locator('.burn-attempt-pending').count() == 1
    page.locator('.burn-showdown-row.loser').wait_for()
    wrong_ms = round((time.perf_counter()-before)*1000)
    page.mouse.up()
    page.wait_for_timeout(180)
    assert page.locator('.burn-showdown-row.loser').count() == 1
    correct = page.locator(f'.board-card[data-owner="{sid}"][data-index="0"]')
    correct.click()
    page.locator('.burn-showdown-row.winner').wait_for()
    page.wait_for_timeout(220)
    assert page.locator('.burn-showdown-row.loser').count() == 1
    page.screenshot(path=str(out/'burn-panel.png'))
    setup(size=4, round_over=True)
    page.locator('#round-results').wait_for(state='visible')
    assert not page.locator('#ability-overlay').is_visible()
    page.screenshot(path=str(out/'round-results.png'))
    for width, height in [(390,844), (844,390)]:
        page.set_viewport_size({'width':width,'height':height})
        setup(size=4, held=True)
        page.screenshot(path=str(out/f'grid-4-{width}.png'))
        overflow = page.evaluate('document.documentElement.scrollWidth > innerWidth')
        assert not overflow
    page.set_viewport_size({'width':1280,'height':800})
    setup(size=4, held=True)
    page.screenshot(path=str(out/'grid-4-1280.png'))
    assert not errors, errors
    print(json.dumps({'errors':errors,'wrong_burn_observed_ms':wrong_ms,'screenshots':8,'card_overlap_checks':'passed','one_attempt_per_press':'passed'}))
    browser.close()
