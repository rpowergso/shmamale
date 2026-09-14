import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from flask import request, jsonify
from app import app, socketio
from game import make_card, make_player, make_slot, reset_discard_burn_state
from multiplayer import rooms, emit_state

@app.post('/qa/reset')
def reset():
    rooms.pop('VISUAL', None)
    return jsonify(ok=True)

@app.post('/qa/setup')
def setup():
    data = request.get_json()
    game = rooms['VISUAL']
    sid = game['host_sid']
    size = data.get('size', 4)
    rows, cols = data.get('rows', size), data.get('cols', size)
    count = data.get('count', 6)
    people = [sid] + [f'qa-{i}' for i in range(1, count)]
    names = ['River', 'Mina', 'Theo', 'Jax', 'Zara', 'Rin']
    game.update(status='playing', phase='choose', turn_index=1, pending_draw=None,
                pending_burn=None, pending_ability=None, held_peek=None, last_action=None,
                burn_showdown=None, burn_history=[], round_results=None, winner_summary=None,
                first_caller_sid=None, final_turns_remaining=[], burnt_slots=[], burn_blockers=[],
                failed_burn_reveals=[], active_burn_contest_id=None, burn_contests={})
    game['settings'].update(grid_rows=rows, grid_cols=cols, grid_peek_modes=['none'] * (rows*cols))
    game['player_order'] = people
    for i, owner in enumerate(people):
        if owner != sid:
            game['players'][owner] = make_player(names[i])
        player = game['players'][owner]
        player.update(username=names[i], called=False, protected=False, first_turn_started=True, opening_peeked=[])
        player['board'] = [make_slot(make_card(['7','9','2','K'][j%4], suit='hearts' if j%2 else 'clubs', deck_number=i*20+j+1)) for j in range(rows*cols)]
    game['discard_pile'] = [make_card('7', suit='hearts', deck_number=200)]
    game['draw_pile'] = [make_card(str(2+i%8), suit='diamonds', deck_number=300+i) for i in range(40)]
    reset_discard_burn_state(game)
    if data.get('my_turn'):
        game['turn_index'] = 0
    if data.get('held'):
        game['pending_draw'] = {'sid': people[1], 'card': game['draw_pile'].pop(), 'source': 'draw'}
        game['phase'] = 'holding'
    if data.get('round_over'):
        game.update(status='round_over', phase='round_over')
        game['round_results'] = {'raw_scores': {x: 12 for x in people}, 'round_scores': {x: 12 for x in people}, 'eliminated': [], 'next_start_sid': sid}
        for player in game['players'].values():
            for slot in player['board']:
                if slot: slot['revealed'] = True
    emit_state('VISUAL')
    return jsonify(sid=sid)

if __name__ == '__main__':
    socketio.run(app, host='127.0.0.1', port=5010, allow_unsafe_werkzeug=True)
