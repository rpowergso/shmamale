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
    page.goto('http://127.0.0.1:5010/homepage')
    page.get_by_role('link', name='HOW TO PLAY').click()
    for index in range(page.locator('[data-tutorial-step]').count()):
        assert page.locator('[data-tutorial-step].active').get_attribute('data-tutorial-step') == str(index)
        if index < page.locator('[data-tutorial-step]').count()-1:
            page.locator('#tutorial-next').click()
    page.request.post('http://127.0.0.1:5010/qa/reset')
    page.goto('http://127.0.0.1:5010/multiplayer/VISUAL')
    page.locator('#username-input').fill('River')
    page.locator('#join-room-btn').click()
    page.locator('#ready-btn').wait_for()
    page.locator('#lobby-settings-toggle').click()
    page.locator('#setting-preset').select_option('madhouse')
    page.wait_for_function('state.settings.preset === "madhouse"')
    assert page.locator('.grid-rule-cell').count() == 6
    page.locator('#setting-preset').select_option('default')
    page.wait_for_function('state.settings.preset === "default"')
    page.locator('#lobby-settings-toggle').click()
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
        assert page.locator('.draw-pile.has-cards').count() == 1
        assert page.locator('.discard-pile.has-cards').count() == 1
        assert page.locator('.draw-pile').evaluate('(el) => getComputedStyle(el, "::after").content') != 'none'
        assert page.locator('.seat').count() == 6
        assert page.locator('.board-card.keyboard-focus').count() == 0
        selectable_outlines = page.locator('.board-card.selectable:not(.opening-peek)').evaluate_all(
            'cards => cards.filter(card => getComputedStyle(card).outlineStyle !== "none").length'
        )
        assert selectable_outlines == 0
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
    page.wait_for_timeout(500)
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
    assert page.locator('.board-card.burn-fail-mark').count() == 1
    wrong_ms = round((time.perf_counter()-before)*1000)
    page.mouse.up()
    page.wait_for_timeout(180)
    assert page.locator('.burn-showdown-row.loser').count() == 1
    correct = page.locator(f'.board-card[data-owner="{sid}"][data-index="0"]')
    click_card(correct)
    page.locator('.burn-showdown-row.winner').wait_for()
    page.wait_for_timeout(220)
    assert page.locator('.burn-showdown-row.winner').count() == 1
    assert page.locator('#burn-showdown').evaluate('(burn) => burn.parentElement.classList.contains("game-rail")')
    assert page.locator('#burn-showdown').bounding_box()['x'] > page.locator('.table-3d').bounding_box()['x']
    page.screenshot(path=str(out/'burn-panel.png'))
    setup(size=4, round_over=True)
    page.locator('#ability-overlay').wait_for(state='visible')
    assert not page.locator('#burn-showdown').is_visible()
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

    # Every supported grid and seat count, including penalties and orientation changes.
    for count in range(2, 7):
        for rows in range(2, 5):
            for cols in range(2, 5):
                setup(count=count, rows=rows, cols=cols)
                assert page.locator('.board-card').count() == count*rows*cols
                missed = page.locator('.board-card').evaluate_all('''cards => cards.filter(card => {
                    const r = card.getBoundingClientRect();
                    return cardAtPoint(r.x+r.width/2, r.y+r.height/2) !== card;
                }).map(c => [c.dataset.owner,c.dataset.index])''')
                assert not missed, (count, rows, cols, missed)

    # A duplicate socket snapshot must not cancel the current flight or replace cards.
    sid = setup(size=2, my_turn=True)
    page.evaluate('window.savedCard = document.querySelector(".board-card")')
    page.locator('#draw-btn').click()
    page.wait_for_function('document.querySelector(".fly-card") !== null')
    result = page.evaluate('''() => {
        const generation = animationGeneration;
        const flight = document.querySelector('.fly-card');
        applyGameState(structuredClone(state));
        return {sameGeneration: generation === animationGeneration, flightAlive: flight.isConnected,
                cardRetained: window.savedCard === document.querySelector('.board-card')};
    }''')
    assert all(result.values()), result
    page.wait_for_function('activeAnimation === null && !document.querySelector(".anim-hidden")')

    # Reduced motion applies state and reveals the held card without a cosmetic flight.
    page.emulate_media(reduced_motion='reduce')
    setup(size=2, my_turn=True)
    page.locator('#draw-btn').click()
    page.wait_for_function('state.phase === "drawn"')
    assert page.locator('.fly-card, .anim-hidden').count() == 0
    page.emulate_media(reduced_motion='no-preference')

    page.locator('#keybinds-btn').click()
    assert page.locator('#keybinds-overlay').is_visible()
    page.keyboard.press('Tab')
    assert page.locator('#keybinds-close').evaluate('(el) => el === document.activeElement')
    page.keyboard.press('Escape')
    assert not page.locator('#keybinds-overlay').is_visible()
    page.locator('#game [data-chat-toggle]').click()
    page.locator('#chat-input').fill('Restored table check')
    page.locator('#chat-send').click()
    page.get_by_text('Restored table check', exact=True).wait_for()
    page.locator('#chat-close').click()

    for rank, ability in [('7','peek_own'), ('9','peek_other'), ('J','switch_unseen'), ('K','switch_peek')]:
        sid = setup(size=2, my_turn=True, draw_rank=rank)
        page.locator('#draw-btn').click()
        page.wait_for_function('state.phase === "drawn" && activeAnimation === null')
        page.keyboard.press('Enter')
        page.wait_for_function('state.phase === "ability" && activeAnimation === null')
        assert page.evaluate('state.pending_ability.type') == ability, (rank, page.evaluate('state.pending_ability'))
        target = sid if ability != 'peek_other' else 'qa-1'
        click_card(page.locator(f'.board-card[data-owner="{target}"][data-index="0"]'))
        if ability.startswith('peek_'):
            page.wait_for_function('Boolean(state.held_peek) && activeAnimation === null')
            page.locator('.held-actions button', has_text='put back').click()
        else:
            page.wait_for_function('state.pending_ability.inspection_count === 1 || state.pending_ability.selected.length === 1')
            click_card(page.locator('.board-card[data-owner="qa-1"][data-index="1"]'))
            if ability == 'switch_peek':
                page.wait_for_function('state.pending_ability?.stage === "deciding"')
                page.locator('[data-ability-tray] button', has_text='Swap').click()
        page.wait_for_function('state.pending_ability === null && activeAnimation === null')
        assert page.locator('.anim-hidden, .fly-card').count() == 0

    for width, height in [(390,844), (844,390), (1024,500), (900,300)]:
        page.set_viewport_size({'width':width,'height':height})
        setup(size=4)
        clipped = page.locator('.board-card').evaluate_all('''cards => cards.filter(c => {
            const r = c.getBoundingClientRect();
            return r.left < 0 || r.right > innerWidth || r.top < 0 || r.bottom > innerHeight;
        }).length''')
        assert clipped == 0, (width, height, clipped)

    # Real touch dispatch on a perspective card.
    touch = browser.new_context(viewport={'width':844,'height':390}, has_touch=True)
    touch_page = touch.new_page()
    touch_page.on('pageerror', lambda error: errors.append(str(error)))
    touch_page.request.post('http://127.0.0.1:5010/qa/reset')
    touch_page.goto('http://127.0.0.1:5010/multiplayer/VISUAL')
    touch_page.locator('#username-input').fill('Touch')
    touch_page.locator('#join-room-btn').click()
    touch_page.locator('#ready-btn').wait_for()
    response = touch_page.request.post('http://127.0.0.1:5010/qa/setup', data={'size':4})
    touch_sid = response.json()['sid']
    touch_page.locator('#game').wait_for(state='visible')
    target = touch_page.locator(f'.board-card[data-owner="{touch_sid}"][data-index="0"]')
    box = target.bounding_box()
    touch_page.touchscreen.tap(box['x'] + box['width']/2, box['y'] + box['height']/2)
    touch_page.locator('.burn-showdown-row.winner').wait_for()
    assert touch_page.locator('.burn-showdown-row').count() == 1
    touch.close()
    assert not errors, errors
    print(json.dumps({'errors':errors,'wrong_burn_observed_ms':wrong_ms,'grid_player_combinations':45,'table_and_card_hit_checks':'passed','one_attempt_per_press':'passed','abilities':'passed','snapshot_continuity':'passed','reduced_motion':'passed'}))
    browser.close()
