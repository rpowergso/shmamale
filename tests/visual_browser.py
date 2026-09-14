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
    def click_card(card):
        box = card.bounding_box()
        page.mouse.click(box['x'] + box['width']/2, box['y'] + box['height']/2)
    def setup(**data):
        response = page.request.post('http://127.0.0.1:5010/qa/setup', data=data)
        assert response.ok, response.text()
        page.locator('#game').wait_for(state='visible')
        page.wait_for_timeout(250)
        return response.json()['sid']
    for size in [4, 3, 2]:
        sid = setup(size=size, held=True)
        page.screenshot(path=str(out / f'grid-{size}-six-players.png'))
        assert page.locator('.felt').is_visible()
        assert page.locator('.table-surface').evaluate('(el) => getComputedStyle(el).transform') != 'none'
        assert page.locator('.seat').count() == 6
        assert page.locator('.board-card.keyboard-focus').count() == 0
        missed = page.locator('.board-card').evaluate_all("cards => cards.filter(card => {\n            const r = card.getBoundingClientRect();\n            return cardAtPoint(r.x+r.width/2, r.y+r.height/2) !== card;\n        }).map(card => ({owner: card.dataset.owner, index: card.dataset.index}))")
        assert not missed, missed
    for rows, cols in [(2,4), (4,2), (3,4)]:
        setup(rows=rows, cols=cols)
        assert page.locator('.seat.me .board-card').count() == rows*cols
        widths = page.locator('.board-card').evaluate_all('(cards) => cards.map(c => c.getBoundingClientRect().width)')
        assert min(widths) >= 15
    sid = setup(size=3, my_turn=True)
    page.keyboard.press('2')
    page.keyboard.press('d')
    assert page.locator('.board-card.keyboard-focus').get_attribute('data-owner') == 'qa-1'
    assert page.locator('.board-card.keyboard-focus').get_attribute('data-index') == '1'
    page.locator('#draw-btn').click()
    page.locator('.hand-tray .held-card').wait_for()
    click_card(page.locator(f'.board-card[data-owner="{sid}"][data-index="0"]'))
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
    click_card(correct)
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
    print(json.dumps({'errors':errors,'wrong_burn_observed_ms':wrong_ms,'screenshots':8,'table_and_card_hit_checks':'passed','one_attempt_per_press':'passed'}))
    browser.close()
