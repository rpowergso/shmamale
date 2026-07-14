from copy import deepcopy
import random

from flask import request
from flask_socketio import emit, join_room

from extensions import socketio
from game import (
    BOTTOM_ROW,
    ability_label,
    build_deck,
    burn_matches,
    card_count,
    deal_board,
    empty_board,
    make_slot,
    public_card,
    score_board,
)


rooms = {}

BOT_NAMES = ["Mina", "Jax", "Rin", "Theo", "Zara"]
BOT_CONFIG = {
    "easy": {
        "reaction": (3.2, 5.2),
        "swap_gain": 6,
        "discard_gain": 5,
        "ability_rate": 0.25,
        "call_score": -1,
        "mistake": 0.35,
    },
    "medium": {
        "reaction": (2.4, 4.2),
        "swap_gain": 3,
        "discard_gain": 2,
        "ability_rate": 0.65,
        "call_score": 1,
        "mistake": 0.12,
    },
    "hard": {
        "reaction": (1.6, 3.0),
        "swap_gain": 1,
        "discard_gain": 1,
        "ability_rate": 0.95,
        "call_score": 0,
        "mistake": 0.03,
    },
}


def default_settings():
    return {"target_score": 50, "deck_count": 1, "jokers": 2, "bot_count": 0, "bot_difficulty": "medium"}


def new_room(host_sid):
    return {
        "status": "lobby",
        "host_sid": host_sid,
        "settings": default_settings(),
        "round_number": 0,
        "players": {},
        "player_order": [],
        "draw_pile": [],
        "discard_pile": [],
        "turn_index": 0,
        "phase": "lobby",
        "pending_draw": None,
        "pending_ability": None,
        "pending_burn": None,
        "held_peek": None,
        "first_caller_sid": None,
        "final_turns_remaining": [],
        "next_start_sid": None,
        "round_results": None,
        "winner_summary": None,
        "action_log": [],
        "bot_mode": False,
        "bot_scheduled_key": None,
        "bot_burn_checked_card_id": None,
        "discard_epoch": 0,
        "burnt_slots": [],
        "burn_blockers": [],
        "last_action": None,
    }


def bot_sid(room, number):
    return f"BOT:{room}:{number}"


def is_bot_player(player):
    return player.get("is_bot", False)


def make_player(username, is_bot=False, difficulty=None):
    return {
        "username": username,
        "ready": is_bot,
        "score": 0,
        "board": empty_board(),
        "called": False,
        "protected": False,
        "first_turn_started": False,
        "opening_peeked": set(),
        "connected": True,
        "is_bot": is_bot,
        "difficulty": difficulty or "",
    }


def configure_bots(game, room, count, difficulty):
    difficulty = difficulty if difficulty in BOT_CONFIG else "medium"
    try:
        count = max(1, min(5, int(count)))
    except (TypeError, ValueError):
        count = 2
    game["bot_mode"] = True
    game["settings"]["bot_count"] = count
    game["settings"]["bot_difficulty"] = difficulty

    existing_bots = [sid for sid, player in game["players"].items() if is_bot_player(player)]
    for sid in existing_bots:
        del game["players"][sid]
        game["player_order"] = [player_sid for player_sid in game["player_order"] if player_sid != sid]

    for number in range(1, count + 1):
        sid = bot_sid(room, number)
        name = f"{BOT_NAMES[(number - 1) % len(BOT_NAMES)]} Bot"
        game["players"][sid] = make_player(name, is_bot=True, difficulty=difficulty)
        game["player_order"].append(sid)


def live_human_sids(game):
    return [
        sid
        for sid in game["player_order"]
        if sid in game["players"] and not is_bot_player(game["players"][sid])
    ]


def find_room_by_sid(sid):
    for room, game in rooms.items():
        if sid in game["players"]:
            return room, game
    return None, None


def player_name(game, sid):
    player = game["players"].get(sid)
    return player["username"] if player else "Player"


def add_log(game, message):
    game["action_log"].append(message)
    game["action_log"] = game["action_log"][-8:]


def slot_key(owner_sid, index):
    return f"{owner_sid}:{index}"


def set_last_action(game, action_type, **payload):
    game["last_action"] = {"type": action_type, "epoch": game.get("discard_epoch", 0), **payload}


def clear_last_action(game):
    game["last_action"] = None


def reset_discard_burn_state(game):
    game["discard_epoch"] = game.get("discard_epoch", 0) + 1
    game["burnt_slots"] = []
    game["burn_blockers"] = []
    game["bot_burn_checked_card_id"] = None


def mark_slot_burnt(game, owner_sid, index):
    key = slot_key(owner_sid, index)
    if key not in game["burnt_slots"]:
        game["burnt_slots"].append(key)


def is_slot_burnt(game, owner_sid, index):
    return slot_key(owner_sid, index) in game.get("burnt_slots", [])


def add_burn_blocker(game, owner_sid, index, card_id):
    game.setdefault("burn_blockers", []).append(
        {"owner_sid": owner_sid, "index": index, "card_id": card_id}
    )


def clear_burn_blockers(game):
    game["burn_blockers"] = []


def is_burn_blocked(game, owner_sid, index, card_id=None):
    for blocker in game.get("burn_blockers", []):
        if blocker["owner_sid"] != owner_sid or blocker["index"] != index:
            continue
        if card_id is None or blocker["card_id"] == card_id:
            return True
    return False


def first_empty_slot(board):
    for index, slot in enumerate(board):
        if not slot or not slot.get("card"):
            return index
    return None


def deal_penalty_card(game, burner_sid):
    """Failed-burn penalty: deal an extra card into an empty slot, or expand the board."""
    if not game["draw_pile"]:
        if len(game["discard_pile"]) > 1:
            recycled = game["discard_pile"][:-1]
            random.shuffle(recycled)
            game["draw_pile"] = recycled
            game["discard_pile"] = [game["discard_pile"][-1]]
        else:
            return None

    card = game["draw_pile"].pop()
    board = game["players"][burner_sid]["board"]
    empty = first_empty_slot(board)
    if empty is not None:
        board[empty] = make_slot(card)
        index = empty
    else:
        # Expand the grid — failed burns add a new slot rather than replacing.
        board.append(make_slot(card))
        index = len(board) - 1

    return {"index": index, "card": public_card(card), "replaced": None, "expanded": empty is None}


def discard_card(game, card, reset_burns=True):
    game["discard_pile"].append(card)
    if reset_burns:
        reset_discard_burn_state(game)


def can_attempt_burn(game, burner_sid):
    if game["status"] != "playing":
        return False, "The round is not active."
    if game.get("pending_burn"):
        return False, "Finish the current burn first."
    if not game["discard_pile"]:
        return False, "There is no discard to burn against."
    pending = game.get("pending_draw")
    if pending and pending.get("sid") == burner_sid:
        return False, "You cannot burn while holding a card."
    held = game.get("held_peek")
    if held and held.get("sid") == burner_sid and held.get("stage") == "holding":
        # Burn from peek is handled by a dedicated event; board burns still blocked while holding.
        pass
    return True, None


def public_burnt_slots(game):
    result = []
    for key in game.get("burnt_slots", []):
        owner_sid, index = key.rsplit(":", 1)
        result.append({"owner_sid": owner_sid, "index": int(index)})
    return result


def current_sid(game):
    if not game["player_order"]:
        return None
    return game["player_order"][game["turn_index"] % len(game["player_order"])]


def live_player_sids(game):
    return [sid for sid in game["player_order"] if sid in game["players"]]


def slot_at(game, owner_sid, index):
    if owner_sid not in game["players"]:
        return None
    if index < 0 or index >= len(game["players"][owner_sid]["board"]):
        return None
    return game["players"][owner_sid]["board"][index]


def visible_slot(game, viewer_sid, owner_sid, index, slot):
    if not slot or not slot.get("card"):
        return {"empty": True, "faceUp": True, "card": None}

    owner = game["players"][owner_sid]
    opening = owner.get("opening_peeked") or set()
    should_show = (
        game["status"] in {"round_over", "game_over"}
        or slot.get("revealed", False)
        or (
            viewer_sid == owner_sid
            and not owner["first_turn_started"]
            and index in opening
        )
    )
    if should_show:
        return {"empty": False, "faceUp": True, "card": public_card(slot["card"])}
    return {"empty": False, "faceUp": False, "card": None}


def player_view(game, viewer_sid):
    players = {}
    for sid in game["player_order"]:
        if sid not in game["players"]:
            continue
        player = game["players"][sid]
        players[sid] = {
            "username": player["username"],
            "ready": player["ready"],
            "is_host": sid == game["host_sid"],
            "is_bot": is_bot_player(player),
            "difficulty": player.get("difficulty", ""),
            "score": player["score"],
            "called": player["called"],
            "protected": player["protected"],
            "connected": player["connected"],
            "first_turn_started": player["first_turn_started"],
            "opening_peekable": (
                sid == viewer_sid
                and not player["first_turn_started"]
                and game["status"] == "playing"
            ),
            "card_count": card_count(player["board"]),
            "board": [
                visible_slot(game, viewer_sid, sid, index, slot)
                for index, slot in enumerate(player["board"])
            ],
        }

    pending_draw = None
    if game["pending_draw"]:
        pending_draw = {
            "sid": game["pending_draw"]["sid"],
            "source": game["pending_draw"]["source"],
            "card": public_card(game["pending_draw"]["card"])
            if game["pending_draw"]["sid"] == viewer_sid
            else None,
        }

    pending_ability = None
    if game["pending_ability"]:
        ability = game["pending_ability"]
        pending_ability = {
            "sid": ability["sid"],
            "type": ability["type"],
            "label": ability_label(ability["type"]),
            "stage": ability["stage"],
            "selected": deepcopy(ability.get("selected", [])) if ability["sid"] == viewer_sid else [],
        }
        if ability["sid"] == viewer_sid:
            if ability.get("peek_result"):
                pending_ability["peek_result"] = deepcopy(ability["peek_result"])
            if ability.get("peek_pair"):
                pending_ability["peek_pair"] = deepcopy(ability["peek_pair"])

    pending_burn = None
    if game["pending_burn"]:
        pending_burn = {
            "sid": game["pending_burn"]["sid"],
            "target_sid": game["pending_burn"]["target_sid"],
            "target_index": game["pending_burn"]["target_index"],
        }

    held_peek = None
    if game.get("held_peek"):
        peek = game["held_peek"]
        held_peek = {
            "sid": peek["sid"],
            "owner_sid": peek["owner_sid"],
            "index": peek["index"],
            "burnable": peek.get("burnable", False),
            "card": public_card(peek["card"]) if peek["sid"] == viewer_sid else None,
        }

    return {
        "status": game["status"],
        "settings": game["settings"],
        "round_number": game["round_number"],
        "players": players,
        "player_order": live_player_sids(game),
        "host_sid": game["host_sid"],
        "viewer_sid": viewer_sid,
        "current_turn_sid": current_sid(game),
        "phase": game["phase"],
        "draw_count": len(game["draw_pile"]),
        "discard_count": len(game["discard_pile"]),
        "discard_top": public_card(game["discard_pile"][-1]) if game["discard_pile"] else None,
        "discard_epoch": game.get("discard_epoch", 0),
        "burnt_slots": public_burnt_slots(game),
        "burn_blockers": [
            {"owner_sid": b["owner_sid"], "index": b["index"]}
            for b in game.get("burn_blockers", [])
        ],
        "pending_draw": pending_draw,
        "pending_ability": pending_ability,
        "pending_burn": pending_burn,
        "held_peek": held_peek,
        "last_action": deepcopy(game.get("last_action")),
        "first_caller_sid": game["first_caller_sid"],
        "final_turns_remaining": list(game["final_turns_remaining"]),
        "round_results": deepcopy(game["round_results"]),
        "winner_summary": deepcopy(game["winner_summary"]),
        "action_log": list(game["action_log"]),
    }


def emit_state(room):
    game = rooms.get(room)
    if not game:
        return
    for sid in live_human_sids(game):
        socketio.emit("game_state", player_view(game, sid), room=sid)
    maybe_schedule_bot_work(room)


def emit_error(message):
    emit("error_message", {"msg": message}, room=request.sid)


def mark_turn_started(game):
    sid = current_sid(game)
    if sid and sid in game["players"]:
        player = game["players"][sid]
        player["first_turn_started"] = True
        player["opening_peeked"] = set()


def start_round(game):
    deck = build_deck(
        deck_count=int(game["settings"]["deck_count"]),
        jokers=int(game["settings"]["jokers"]),
    )
    for sid in game["player_order"]:
        player = game["players"][sid]
        player["board"] = deal_board(deck)
        player["ready"] = False
        player["called"] = False
        player["protected"] = False
        player["first_turn_started"] = False
        player["opening_peeked"] = set()

    game["round_number"] += 1
    game["draw_pile"] = deck
    game["discard_pile"] = []
    game["phase"] = "choose"
    game["pending_draw"] = None
    game["pending_ability"] = None
    game["pending_burn"] = None
    game["held_peek"] = None
    game["first_caller_sid"] = None
    game["final_turns_remaining"] = []
    game["round_results"] = None
    game["winner_summary"] = None
    game["action_log"] = []
    game["discard_epoch"] = 0
    game["burnt_slots"] = []
    game["burn_blockers"] = []
    game["last_action"] = None
    game["bot_burn_checked_card_id"] = None

    if game["next_start_sid"] in game["player_order"]:
        game["turn_index"] = game["player_order"].index(game["next_start_sid"])
    else:
        game["turn_index"] = 0

    game["status"] = "playing"
    # The starting player is intentionally NOT marked here so they still get the opening
    # peek at their two bottom cards until they take their first action. Nobody has acted
    # yet, so their board can't have been altered — the peek is safe.
    add_log(game, f"Round {game['round_number']} started. {player_name(game, current_sid(game))} goes first.")


def advance_turn(game):
    previous_sid = current_sid(game)
    if previous_sid in game["final_turns_remaining"]:
        game["final_turns_remaining"].remove(previous_sid)

    if game["first_caller_sid"] and not game["final_turns_remaining"]:
        finish_round(game)
        return

    if not game["player_order"]:
        return

    for _ in range(len(game["player_order"])):
        game["turn_index"] = (game["turn_index"] + 1) % len(game["player_order"])
        next_sid = current_sid(game)
        if game["first_caller_sid"] and next_sid not in game["final_turns_remaining"]:
            continue
        break

    game["phase"] = "choose"
    game["pending_draw"] = None
    game["pending_ability"] = None
    game["held_peek"] = None
    mark_turn_started(game)


def normalized_round_scores(raw_scores):
    min_score = min(raw_scores.values()) if raw_scores else 0
    bump = abs(min_score) if min_score < 0 else 0
    return {sid: score + bump for sid, score in raw_scores.items()}, bump


def choose_low_tie_starter(game, low_sids):
    if not low_sids:
        return game["player_order"][0]
    if game["first_caller_sid"] in low_sids and len(low_sids) > 1:
        candidates = [sid for sid in low_sids if sid != game["first_caller_sid"]]
    else:
        candidates = list(low_sids)

    max_cards = max(card_count(game["players"][sid]["board"]) for sid in candidates)
    candidates = [sid for sid in candidates if card_count(game["players"][sid]["board"]) == max_cards]
    for sid in game["player_order"]:
        if sid in candidates:
            return sid
    return candidates[0]


def finish_round(game):
    hand_scores = {sid: score_board(game["players"][sid]["board"]) for sid in game["player_order"]}
    caller_sid = game["first_caller_sid"]

    min_hand = min(hand_scores.values())
    max_hand = max(hand_scores.values())
    low_sids = [sid for sid, score in hand_scores.items() if score == min_hand]
    game["next_start_sid"] = choose_low_tie_starter(game, low_sids)

    round_scores, negative_bump = normalized_round_scores(hand_scores)
    doubled_caller = False
    if caller_sid in hand_scores and hand_scores[caller_sid] == max_hand and max_hand > min_hand:
        round_scores[caller_sid] *= 2
        doubled_caller = True

    for sid, score in round_scores.items():
        game["players"][sid]["score"] += score

    target_score = int(game["settings"]["target_score"])
    over_target = [sid for sid in game["player_order"] if game["players"][sid]["score"] >= target_score]

    game["round_results"] = {
        "raw_scores": hand_scores,
        "round_scores": round_scores,
        "negative_bump": negative_bump,
        "doubled_caller": doubled_caller,
        "caller_sid": caller_sid,
        "next_start_sid": game["next_start_sid"],
        "over_target": over_target,
    }
    game["phase"] = "round_over"
    game["pending_draw"] = None
    game["pending_ability"] = None
    game["pending_burn"] = None
    game["held_peek"] = None
    clear_burn_blockers(game)
    clear_last_action(game)

    if over_target:
        highest = max(game["players"][sid]["score"] for sid in over_target)
        losers = [sid for sid in over_target if game["players"][sid]["score"] == highest]
        game["status"] = "game_over"
        game["winner_summary"] = {"losers": losers, "target_score": target_score}
        add_log(game, "Game over. First player to the target loses.")
    else:
        game["status"] = "round_over"
        add_log(game, f"Round ended. {player_name(game, game['next_start_sid'])} starts next.")


def ensure_turn(game):
    if game["status"] != "playing":
        emit_error("The round is not active.")
        return False
    if current_sid(game) != request.sid:
        emit_error("It is not your turn.")
        return False
    return True


def ensure_no_pending_burn(game):
    if game.get("pending_burn"):
        emit_error("Finish the burn first.")
        return False
    return True


def protected_from_switch(game, actor_sid, owner_sid):
    return owner_sid != actor_sid and game["players"][owner_sid].get("protected", False)


def bot_config(player):
    return BOT_CONFIG.get(player.get("difficulty"), BOT_CONFIG["medium"])


def live_slots_for(game, sid):
    return [
        (index, slot)
        for index, slot in enumerate(game["players"][sid]["board"])
        if slot and slot.get("card")
    ]


def all_switchable_slots(game, actor_sid):
    slots = []
    for owner_sid in game["player_order"]:
        if owner_sid not in game["players"]:
            continue
        if protected_from_switch(game, actor_sid, owner_sid):
            continue
        for index, slot in live_slots_for(game, owner_sid):
            slots.append((owner_sid, index, slot))
    return slots


def best_swap_slot(game, sid, new_card, difficulty):
    own_slots = live_slots_for(game, sid)
    if not own_slots:
        return None, 0
    if difficulty == "easy" and random.random() < 0.28:
        index, slot = random.choice(own_slots)
        return index, slot["card"]["value"] - new_card["value"]
    index, slot = max(own_slots, key=lambda item: item[1]["card"]["value"])
    return index, slot["card"]["value"] - new_card["value"]


def swap_slots(game, first_owner, first_index, second_owner, second_index):
    first_board = game["players"][first_owner]["board"]
    second_board = game["players"][second_owner]["board"]
    first_board[first_index], second_board[second_index] = (
        second_board[second_index],
        first_board[first_index],
    )


def bot_action_key(game):
    pending_burn = game.get("pending_burn")
    if pending_burn and pending_burn["sid"] in game["players"]:
        player = game["players"][pending_burn["sid"]]
        if is_bot_player(player):
            return (
                "burn_give",
                pending_burn["sid"],
                pending_burn["target_sid"],
                pending_burn["target_index"],
                pending_burn["target_card_id"],
            )

    if game["status"] != "playing":
        return None

    sid = current_sid(game)
    if sid not in game["players"] or not is_bot_player(game["players"][sid]):
        return None

    if game["phase"] == "choose":
        top_id = game["discard_pile"][-1]["id"] if game["discard_pile"] else ""
        return ("choose", sid, len(game["draw_pile"]), top_id, game["first_caller_sid"] or "")
    if game["phase"] == "drawn" and game.get("pending_draw", {}).get("sid") == sid:
        pending = game["pending_draw"]
        return ("drawn", sid, pending["source"], pending["card"]["id"])
    if game.get("held_peek") and game["held_peek"].get("sid") == sid:
        peek = game["held_peek"]
        return ("held_peek", sid, peek["owner_sid"], peek["index"], peek["card"]["id"])
    if game["phase"] == "ability" and game.get("pending_ability", {}).get("sid") == sid:
        ability = game["pending_ability"]
        if ability.get("stage") == "holding":
            return None
        return (
            "ability",
            sid,
            ability["type"],
            ability["stage"],
            len(ability.get("selected", [])),
        )
    return None


def maybe_schedule_bot_work(room):
    game = rooms.get(room)
    if not game:
        return

    maybe_schedule_bot_burn(room)

    key = bot_action_key(game)
    if not key or game.get("bot_scheduled_key") == key:
        return
    game["bot_scheduled_key"] = key
    socketio.start_background_task(run_bot_work, room, key)


def run_bot_work(room, key):
    game = rooms.get(room)
    sid = key[1]
    if not game or sid not in game["players"]:
        return

    low, high = bot_config(game["players"][sid])["reaction"]
    # Peeks need a longer visible hold so humans see the lift / empty slot.
    if key[0] == "held_peek":
        # Leave the lifted card visible long enough after the fly animation (~1s).
        low, high = 2.2, 3.4
    elif key[0] == "ability":
        # Pause on "choosing" before the peek/switch resolves.
        low, high = max(low * 0.7, 1.2), max(high * 0.85, 2.2)
    socketio.sleep(random.uniform(low, high))

    game = rooms.get(room)
    if not game or game.get("bot_scheduled_key") != key:
        return

    game["bot_scheduled_key"] = None
    if key[0] == "burn_give":
        perform_bot_burn_give(game, sid)
    elif key[0] == "choose":
        perform_bot_choose(game, sid)
    elif key[0] == "drawn":
        perform_bot_drawn(game, sid)
    elif key[0] == "ability":
        perform_bot_ability(game, sid)
    elif key[0] == "held_peek":
        perform_bot_put_back(game, sid)
    emit_state(room)


def maybe_schedule_bot_burn(room):
    game = rooms.get(room)
    if not game or game["status"] != "playing" or game.get("pending_burn"):
        return
    if game["phase"] not in {"choose", "ability"}:
        return
    if game.get("pending_draw"):
        return
    if not game["discard_pile"]:
        return

    top_card = game["discard_pile"][-1]
    if game.get("bot_burn_checked_card_id") == top_card["id"]:
        return

    candidate = choose_bot_burn_candidate(game, top_card)
    game["bot_burn_checked_card_id"] = top_card["id"]
    if not candidate:
        return

    bot_player = game["players"][candidate["sid"]]
    low, high = bot_config(bot_player)["reaction"]
    socketio.start_background_task(
        run_bot_burn,
        room,
        top_card["id"],
        candidate,
        random.uniform(low, high),
    )


def run_bot_burn(room, discard_card_id, candidate, delay):
    socketio.sleep(delay)
    game = rooms.get(room)
    if (
        not game
        or game["status"] != "playing"
        or game.get("pending_burn")
        or not game["discard_pile"]
        or game["discard_pile"][-1]["id"] != discard_card_id
    ):
        return
    perform_bot_burn(game, candidate)
    emit_state(room)


def choose_bot_burn_candidate(game, top_card):
    bot_sids = [
        sid
        for sid in game["player_order"]
        if sid in game["players"] and is_bot_player(game["players"][sid])
    ]
    random.shuffle(bot_sids)

    for sid in bot_sids:
        if game.get("pending_draw") and game["pending_draw"].get("sid") == sid:
            continue
        player = game["players"][sid]
        difficulty = player["difficulty"]
        config = bot_config(player)
        if random.random() < config["mistake"]:
            continue

        own_matches = [
            (index, slot)
            for index, slot in live_slots_for(game, sid)
            if burn_matches(top_card, slot["card"])
            and not is_slot_burnt(game, sid, index)
            and not is_burn_blocked(game, sid, index, slot["card"]["id"])
        ]
        if own_matches:
            index, slot = max(own_matches, key=lambda item: item[1]["card"]["value"])
            value = slot["card"]["value"]
            if (
                (difficulty == "easy" and value >= 9)
                or (difficulty == "medium" and value >= 5)
                or (difficulty == "hard" and value >= 1)
            ):
                return {"sid": sid, "owner_sid": sid, "index": index}

        give_slot = None
        own_slots = live_slots_for(game, sid)
        if own_slots:
            give_slot = max(own_slots, key=lambda item: item[1]["card"]["value"])
        if not give_slot:
            continue

        opponent_matches = []
        for owner_sid in game["player_order"]:
            if owner_sid == sid or owner_sid not in game["players"]:
                continue
            for index, slot in live_slots_for(game, owner_sid):
                if not slot.get("revealed"):
                    continue
                if is_slot_burnt(game, owner_sid, index):
                    continue
                if burn_matches(top_card, slot["card"]):
                    opponent_matches.append((owner_sid, index, slot))

        if not opponent_matches:
            continue
        owner_sid, index, target_slot = min(opponent_matches, key=lambda item: item[2]["card"]["value"])
        give_value = give_slot[1]["card"]["value"]
        target_value = target_slot["card"]["value"]
        threshold = {"easy": 99, "medium": 5, "hard": 0}[difficulty]
        if give_value - target_value >= threshold:
            return {"sid": sid, "owner_sid": owner_sid, "index": index}
    return None


def apply_successful_own_burn(game, burner_sid, owner_sid, index, target_card):
    game["players"][owner_sid]["board"][index] = None
    mark_slot_burnt(game, owner_sid, index)
    set_last_action(
        game,
        "burn",
        sid=burner_sid,
        owner_sid=owner_sid,
        index=index,
        card=public_card(target_card),
        own=True,
    )
    add_log(game, f"{player_name(game, burner_sid)} burned their own {target_card['label']}.")


def apply_successful_opponent_burn(game, burner_sid, owner_sid, index, target_card):
    if card_count(game["players"][burner_sid]["board"]) == 0:
        return False, "You need one of your own cards to give them."
    mark_slot_burnt(game, owner_sid, index)
    game["pending_burn"] = {
        "sid": burner_sid,
        "target_sid": owner_sid,
        "target_index": index,
        "target_card_id": target_card["id"],
    }
    set_last_action(
        game,
        "burn",
        sid=burner_sid,
        owner_sid=owner_sid,
        index=index,
        card=public_card(target_card),
        own=False,
    )
    add_log(
        game,
        f"{player_name(game, burner_sid)} hit a burn on {player_name(game, owner_sid)}.",
    )
    return True, None


def apply_failed_burn(game, burner_sid, owner_sid, index, target_card, reason):
    slot = slot_at(game, owner_sid, index)
    if slot:
        slot["revealed"] = True
    penalty = deal_penalty_card(game, burner_sid)
    set_last_action(
        game,
        "burn_fail",
        sid=burner_sid,
        owner_sid=owner_sid,
        index=index,
        card=public_card(target_card),
        reason=reason,
        penalty=penalty,
    )
    penalty_note = ""
    if penalty:
        penalty_note = f" Penalty card dealt to slot {penalty['index'] + 1}."
    add_log(
        game,
        f"{player_name(game, burner_sid)} missed a burn on {player_name(game, owner_sid)}'s card.{penalty_note}",
    )


def perform_bot_burn(game, candidate):
    sid = candidate["sid"]
    owner_sid = candidate["owner_sid"]
    index = candidate["index"]
    ok, _ = can_attempt_burn(game, sid)
    if not ok:
        return
    slot = slot_at(game, owner_sid, index)
    if not slot or not slot.get("card"):
        return
    if is_slot_burnt(game, owner_sid, index):
        return
    if is_burn_blocked(game, owner_sid, index, slot["card"]["id"]):
        return

    target_card = slot["card"]
    slot["revealed"] = True
    if not burn_matches(game["discard_pile"][-1], target_card):
        apply_failed_burn(game, sid, owner_sid, index, target_card, "rank")
        return

    if owner_sid == sid:
        apply_successful_own_burn(game, sid, owner_sid, index, target_card)
        return

    apply_successful_opponent_burn(game, sid, owner_sid, index, target_card)


def perform_bot_burn_give(game, sid):
    pending = game.get("pending_burn")
    if not pending or pending["sid"] != sid:
        return
    target_slot = slot_at(game, pending["target_sid"], pending["target_index"])
    own_slots = live_slots_for(game, sid)
    if not target_slot or not target_slot.get("card") or not own_slots:
        game["pending_burn"] = None
        return

    give_index, give_slot = max(own_slots, key=lambda item: item[1]["card"]["value"])
    given_label = give_slot["card"]["label"]
    burned_label = target_slot["card"]["label"]
    game["players"][pending["target_sid"]]["board"][pending["target_index"]] = {
        "card": give_slot["card"],
        "revealed": False,
    }
    game["players"][sid]["board"][give_index] = None
    game["pending_burn"] = None
    set_last_action(
        game,
        "burn_give",
        sid=sid,
        target_sid=pending["target_sid"],
        target_index=pending["target_index"],
        give_index=give_index,
    )
    add_log(
        game,
        f"{player_name(game, sid)} burned {burned_label} and gave {given_label} to {player_name(game, pending['target_sid'])}.",
    )
    if game.pop("_advance_after_burn_give", False):
        advance_turn(game)


def bot_should_call(game, sid):
    player = game["players"][sid]
    difficulty = player["difficulty"]
    score = score_board(player["board"])
    cards = card_count(player["board"])

    if player["called"]:
        return False
    if game["first_caller_sid"]:
        if difficulty == "easy":
            return score <= -1 and random.random() < 0.35
        if difficulty == "medium":
            return score <= 5
        return score <= 7
    if difficulty == "easy":
        return score <= -1 and random.random() < 0.45
    if difficulty == "medium":
        return score <= 1 or (cards <= 2 and score <= 4)
    return score <= 0 or (cards <= 3 and score <= 3)


def perform_bot_choose(game, sid):
    if current_sid(game) != sid or game["phase"] != "choose":
        return

    mark_turn_started(game)
    if bot_should_call(game, sid):
        perform_bot_call(game, sid)
        return

    player = game["players"][sid]
    config = bot_config(player)
    difficulty = player["difficulty"]

    if game["discard_pile"]:
        top_card = game["discard_pile"][-1]
        _, gain = best_swap_slot(game, sid, top_card, difficulty)
        should_take = gain >= config["discard_gain"]
        if difficulty == "hard" and top_card["value"] <= 0 and gain >= 0:
            should_take = True
        if difficulty == "easy" and top_card["value"] <= 1 and gain > 0:
            should_take = True
        if random.random() < config["mistake"]:
            should_take = not should_take and random.random() < 0.4
        if should_take:
            card = game["discard_pile"].pop()
            reset_discard_burn_state(game)
            game["pending_draw"] = {"sid": sid, "card": card, "source": "discard"}
            game["phase"] = "drawn"
            set_last_action(game, "take", sid=sid, card=public_card(card))
            add_log(game, f"{player_name(game, sid)} took the discard.")
            return

    if game["draw_pile"]:
        card = game["draw_pile"].pop()
        game["pending_draw"] = {"sid": sid, "card": card, "source": "draw"}
        game["phase"] = "drawn"
        set_last_action(game, "draw", sid=sid)
        add_log(game, f"{player_name(game, sid)} drew from the deck.")
        return

    if game["discard_pile"]:
        card = game["discard_pile"].pop()
        reset_discard_burn_state(game)
        game["pending_draw"] = {"sid": sid, "card": card, "source": "discard"}
        game["phase"] = "drawn"
        set_last_action(game, "take", sid=sid, card=public_card(card))
        add_log(game, f"{player_name(game, sid)} took the last discard.")
        return

    perform_bot_call(game, sid)


def perform_bot_drawn(game, sid):
    pending = game.get("pending_draw")
    if current_sid(game) != sid or game["phase"] != "drawn" or not pending or pending["sid"] != sid:
        return

    player = game["players"][sid]
    config = bot_config(player)
    difficulty = player["difficulty"]
    card = pending["card"]
    index, gain = best_swap_slot(game, sid, card, difficulty)

    if pending["source"] == "discard":
        if index is not None:
            bot_swap_drawn(game, sid, index)
        return

    should_swap = index is not None and (
        gain >= config["swap_gain"] or (card["value"] <= 0 and gain >= 0)
    )
    if card["ability"] and card["value"] >= 7 and random.random() < config["ability_rate"]:
        should_swap = False
    if random.random() < config["mistake"]:
        should_swap = not should_swap

    if should_swap and index is not None:
        bot_swap_drawn(game, sid, index)
    else:
        bot_play_drawn(game, sid)


def bot_swap_drawn(game, sid, index):
    pending = game["pending_draw"]
    slot = slot_at(game, sid, index)
    if not slot or not slot.get("card"):
        bot_play_drawn(game, sid)
        return

    old_card = slot["card"]
    source = pending["source"]
    new_card = pending["card"]
    game["players"][sid]["board"][index] = make_slot(new_card)
    discard_card(game, old_card)
    game["pending_draw"] = None
    if source == "discard":
        add_burn_blocker(game, sid, index, new_card["id"])
    set_last_action(
        game,
        "swap",
        sid=sid,
        index=index,
        source=source,
        outgoing=public_card(old_card),
    )
    add_log(game, f"{player_name(game, sid)} switched a card and discarded {old_card['label']}.")
    advance_turn(game)


def bot_play_drawn(game, sid):
    pending = game["pending_draw"]
    card = pending["card"]
    discard_card(game, card)
    game["pending_draw"] = None
    set_last_action(game, "play", sid=sid, card=public_card(card))
    add_log(game, f"{player_name(game, sid)} played {card['label']} to discard.")

    player = game["players"][sid]
    if card["ability"] and random.random() < bot_config(player)["ability_rate"]:
        game["phase"] = "ability"
        game["pending_ability"] = {
            "sid": sid,
            "type": card["ability"],
            "stage": "selecting",
            "selected": [],
            "card": card,
        }
    else:
        if card["ability"]:
            add_log(game, f"{player_name(game, sid)} skipped the special.")
        advance_turn(game)


def perform_bot_put_back(game, sid):
    peek = game.get("held_peek")
    if not peek or peek["sid"] != sid:
        return
    # Hard bots burn peeked matches when eligible.
    if (
        peek.get("burnable")
        and game["discard_pile"]
        and burn_matches(game["discard_pile"][-1], peek["card"])
        and bot_config(game["players"][sid])["ability_rate"] > 0.5
    ):
        owner_sid = peek["owner_sid"]
        index = peek["index"]
        target_card = peek["card"]
        game["held_peek"] = None
        game["pending_ability"] = None
        if owner_sid == sid:
            mark_slot_burnt(game, owner_sid, index)
            set_last_action(
                game,
                "burn",
                sid=sid,
                owner_sid=owner_sid,
                index=index,
                card=public_card(target_card),
                own=True,
                from_peek=True,
            )
            add_log(game, f"{player_name(game, sid)} burned their peeked {target_card['label']}.")
            advance_turn(game)
            return
        game["players"][owner_sid]["board"][index] = {"card": target_card, "revealed": True}
        ok, _ = apply_successful_opponent_burn(game, sid, owner_sid, index, target_card)
        if ok:
            game["_advance_after_burn_give"] = True
            return
    put_back_held_peek(game)


def put_back_held_peek(game):
    peek = game.get("held_peek")
    if not peek:
        return
    owner_sid = peek["owner_sid"]
    index = peek["index"]
    board = game["players"][owner_sid]["board"]
    # Slot was emptied while holding; restore face-down (or revealed if it was).
    board[index] = {"card": peek["card"], "revealed": peek.get("was_revealed", False)}
    set_last_action(
        game,
        "put_back",
        sid=peek["sid"],
        owner_sid=owner_sid,
        index=index,
    )
    game["held_peek"] = None
    game["pending_ability"] = None
    advance_turn(game)


def perform_bot_ability(game, sid):
    ability = game.get("pending_ability")
    if current_sid(game) != sid or game["phase"] != "ability" or not ability or ability["sid"] != sid:
        return
    if game.get("pending_burn"):
        return

    if ability["stage"] == "waiting":
        ability["stage"] = "selecting"
        ability["selected"] = []

    ability_type = ability["type"]
    if ability_type == "peek_own":
        own_slots = live_slots_for(game, sid)
        if own_slots:
            index, slot = max(own_slots, key=lambda item: item[1]["card"]["value"])
            begin_held_peek(game, sid, sid, index)
            # Bots put back immediately via held_peek scheduling.
        else:
            game["pending_ability"] = None
            advance_turn(game)
        return

    if ability_type == "peek_other":
        targets = [
            (owner_sid, index, slot)
            for owner_sid in game["player_order"]
            if owner_sid != sid and owner_sid in game["players"]
            for index, slot in live_slots_for(game, owner_sid)
        ]
        if targets:
            owner_sid, index, _ = random.choice(targets)
            begin_held_peek(game, sid, owner_sid, index)
        else:
            game["pending_ability"] = None
            advance_turn(game)
        return

    if ability_type in {"switch_unseen", "switch_peek"}:
        pair = choose_bot_switch_pair(game, sid, ability_type == "switch_peek")
        if pair:
            first, second, should_switch = pair
            if should_switch:
                swap_slots(game, first[0], first[1], second[0], second[1])
                set_last_action(
                    game,
                    "switch",
                    sid=sid,
                    a={"owner_sid": first[0], "index": first[1]},
                    b={"owner_sid": second[0], "index": second[1]},
                )
                if ability_type == "switch_peek":
                    add_log(game, f"{player_name(game, sid)} looked and switched two cards.")
                else:
                    add_log(game, f"{player_name(game, sid)} switched two unseen cards.")
            else:
                add_log(game, f"{player_name(game, sid)} looked and kept the cards in place.")
        else:
            add_log(game, f"{player_name(game, sid)} skipped the special.")
        game["pending_ability"] = None
        advance_turn(game)


def begin_held_peek(game, viewer_sid, owner_sid, index):
    slot = slot_at(game, owner_sid, index)
    if not slot or not slot.get("card"):
        return False
    card = slot["card"]
    was_revealed = slot.get("revealed", False)
    game["players"][owner_sid]["board"][index] = None
    burnable = bool(
        game["discard_pile"]
        and burn_matches(game["discard_pile"][-1], card)
        and not is_slot_burnt(game, owner_sid, index)
        and not is_burn_blocked(game, owner_sid, index, card["id"])
    )
    game["held_peek"] = {
        "sid": viewer_sid,
        "owner_sid": owner_sid,
        "index": index,
        "card": card,
        "was_revealed": was_revealed,
        "burnable": burnable,
    }
    game["pending_ability"] = {
        "sid": viewer_sid,
        "type": "peek_own" if owner_sid == viewer_sid else "peek_other",
        "stage": "holding",
        "selected": [{"owner_sid": owner_sid, "index": index}],
        "card": game["pending_ability"]["card"] if game.get("pending_ability") else None,
    }
    set_last_action(
        game,
        "peek",
        sid=viewer_sid,
        owner_sid=owner_sid,
        index=index,
    )
    owner_label = "their own" if owner_sid == viewer_sid else f"{player_name(game, owner_sid)}'s"
    add_log(game, f"{player_name(game, viewer_sid)} peeked at {owner_label} card.")
    return True


def choose_bot_switch_pair(game, sid, can_peek):
    player = game["players"][sid]
    difficulty = player["difficulty"]
    slots = all_switchable_slots(game, sid)
    if len(slots) < 2:
        return None

    if difficulty == "easy":
        first, second = random.sample(slots, 2)
        return ((first[0], first[1]), (second[0], second[1]), random.random() < 0.55)

    own_slots = [(sid, index, slot) for index, slot in live_slots_for(game, sid)]
    opponent_slots = [
        item
        for item in slots
        if item[0] != sid
    ]
    if not own_slots or not opponent_slots:
        first, second = random.sample(slots, 2)
        return ((first[0], first[1]), (second[0], second[1]), can_peek and random.random() < 0.5)

    own_high = max(own_slots, key=lambda item: item[2]["card"]["value"])
    if difficulty == "medium":
        opponent_choice = random.choice(opponent_slots)
        should_switch = own_high[2]["card"]["value"] >= 8
        return (
            (own_high[0], own_high[1]),
            (opponent_choice[0], opponent_choice[1]),
            should_switch,
        )

    opponent_low = min(opponent_slots, key=lambda item: item[2]["card"]["value"])
    should_switch = own_high[2]["card"]["value"] > opponent_low[2]["card"]["value"]
    return (
        (own_high[0], own_high[1]),
        (opponent_low[0], opponent_low[1]),
        should_switch,
    )


def perform_bot_call(game, sid):
    player = game["players"][sid]
    player["called"] = True
    player["protected"] = True
    if not game["first_caller_sid"]:
        game["first_caller_sid"] = sid
        game["final_turns_remaining"] = [player_sid for player_sid in game["player_order"] if player_sid != sid]
        add_log(game, f"{player_name(game, sid)} called. Everyone else gets one final turn.")
    else:
        add_log(game, f"{player_name(game, sid)} called to protect their cards.")
    advance_turn(game)


@socketio.on("join")
def on_join(data):
    room = data["room"].upper()
    username = data.get("username", "Anonymous").strip() or "Anonymous"
    join_room(room)

    if room not in rooms:
        rooms[room] = new_room(request.sid)

    game = rooms[room]
    if request.sid not in game["players"]:
        game["players"][request.sid] = make_player(username)
        game["player_order"].append(request.sid)
    else:
        game["players"][request.sid]["username"] = username
        game["players"][request.sid]["connected"] = True

    if (
        game["status"] == "lobby"
        and request.sid == game["host_sid"]
        and data.get("bot_mode")
    ):
        configure_bots(
            game,
            room,
            data.get("bot_count", 2),
            data.get("bot_difficulty", "medium"),
        )
        game["players"][request.sid]["ready"] = True
        start_round(game)

    emit_state(room)


@socketio.on("update_settings")
def on_update_settings(data):
    room = data["room"].upper()
    game = rooms.get(room)
    if not game or request.sid != game["host_sid"] or game["status"] != "lobby":
        return

    target_score = max(10, min(500, int(data.get("target_score", 50))))
    deck_count = max(1, min(4, int(data.get("deck_count", 1))))
    jokers = max(0, min(8, int(data.get("jokers", 2))))
    game["settings"].update(
        {"target_score": target_score, "deck_count": deck_count, "jokers": jokers}
    )
    emit_state(room)


@socketio.on("toggle_ready")
def on_toggle_ready(data):
    room = data["room"].upper()
    game = rooms.get(room)
    if not game or request.sid not in game["players"] or game["status"] != "lobby":
        return
    game["players"][request.sid]["ready"] = not game["players"][request.sid]["ready"]
    emit_state(room)


@socketio.on("start_game")
def on_start_game(data):
    room = data["room"].upper()
    game = rooms.get(room)
    if not game:
        emit_error("Room not found.")
        return
    if request.sid != game["host_sid"]:
        emit_error("Only the host can start the game.")
        return
    if len(game["players"]) < 2:
        emit_error("You need at least 2 players.")
        return
    if not all(player["ready"] for player in game["players"].values()):
        emit_error("Everyone needs to be ready.")
        return
    start_round(game)
    emit_state(room)


@socketio.on("next_round")
def on_next_round(data):
    room = data["room"].upper()
    game = rooms.get(room)
    if not game:
        return
    if request.sid != game["host_sid"]:
        emit_error("Only the host can start the next round.")
        return
    if game["status"] != "round_over":
        return
    start_round(game)
    emit_state(room)


@socketio.on("peek_opening")
def on_peek_opening(data):
    """Click-to-reveal one of your two bottom cards before your first turn action."""
    room = data["room"].upper()
    game = rooms.get(room)
    if not game or game["status"] != "playing":
        return
    if request.sid not in game["players"]:
        return
    player = game["players"][request.sid]
    if player["first_turn_started"]:
        emit_error("Opening peek is over.")
        return
    index = int(data.get("index", -1))
    if index not in BOTTOM_ROW:
        emit_error("You can only peek your two bottom cards at the start.")
        return
    slot = slot_at(game, request.sid, index)
    if not slot or not slot.get("card"):
        emit_error("That slot is empty.")
        return
    peeked = player.setdefault("opening_peeked", set())
    peeked.add(index)
    emit_state(room)


@socketio.on("draw_from_deck")
def on_draw_from_deck(data):
    room = data["room"].upper()
    game = rooms.get(room)
    if not game or not ensure_turn(game):
        return
    if not ensure_no_pending_burn(game):
        return
    if game["phase"] != "choose":
        emit_error("Finish the current action first.")
        return
    if not game["draw_pile"]:
        emit_error("The draw pile is empty.")
        return

    mark_turn_started(game)
    card = game["draw_pile"].pop()
    game["pending_draw"] = {"sid": request.sid, "card": card, "source": "draw"}
    game["phase"] = "drawn"
    set_last_action(game, "draw", sid=request.sid)
    add_log(game, f"{player_name(game, request.sid)} drew from the deck.")
    emit_state(room)


@socketio.on("take_discard")
def on_take_discard(data):
    room = data["room"].upper()
    game = rooms.get(room)
    if not game or not ensure_turn(game):
        return
    if not ensure_no_pending_burn(game):
        return
    if game["phase"] != "choose":
        emit_error("Finish the current action first.")
        return
    if not game["discard_pile"]:
        emit_error("The discard pile is empty.")
        return

    mark_turn_started(game)
    card = game["discard_pile"].pop()
    # Taking the discard exposes a new top — reset burn tracking for the new top.
    reset_discard_burn_state(game)
    game["pending_draw"] = {"sid": request.sid, "card": card, "source": "discard"}
    game["phase"] = "drawn"
    set_last_action(game, "take", sid=request.sid, card=public_card(card))
    add_log(game, f"{player_name(game, request.sid)} took the discard.")
    emit_state(room)


@socketio.on("swap_drawn")
def on_swap_drawn(data):
    room = data["room"].upper()
    game = rooms.get(room)
    if not game or not ensure_turn(game):
        return
    if not ensure_no_pending_burn(game):
        return
    pending = game["pending_draw"]
    if game["phase"] != "drawn" or not pending or pending["sid"] != request.sid:
        emit_error("You do not have a card to switch.")
        return

    index = int(data.get("index", -1))
    slot = slot_at(game, request.sid, index)
    if not slot or not slot.get("card"):
        emit_error("Choose one of your live cards.")
        return

    old_card = slot["card"]
    source = pending["source"]
    new_card = pending["card"]
    game["players"][request.sid]["board"][index] = make_slot(new_card)
    discard_card(game, old_card)
    game["pending_draw"] = None
    if source == "discard":
        add_burn_blocker(game, request.sid, index, new_card["id"])
    set_last_action(
        game,
        "swap",
        sid=request.sid,
        index=index,
        source=source,
        outgoing=public_card(old_card),
    )
    add_log(game, f"{player_name(game, request.sid)} switched a card and discarded {old_card['label']}.")
    advance_turn(game)
    emit_state(room)


@socketio.on("play_drawn")
def on_play_drawn(data):
    room = data["room"].upper()
    game = rooms.get(room)
    if not game or not ensure_turn(game):
        return
    if not ensure_no_pending_burn(game):
        return
    pending = game["pending_draw"]
    if game["phase"] != "drawn" or not pending or pending["sid"] != request.sid:
        emit_error("You do not have a card to play.")
        return
    if pending["source"] != "draw":
        emit_error("Only cards drawn from the deck can be played.")
        return

    card = pending["card"]
    discard_card(game, card)
    game["pending_draw"] = None
    set_last_action(game, "play", sid=request.sid, card=public_card(card))
    add_log(game, f"{player_name(game, request.sid)} played {card['label']} to discard.")
    if card["ability"]:
        game["phase"] = "ability"
        game["pending_ability"] = {
            "sid": request.sid,
            "type": card["ability"],
            "stage": "selecting",
            "selected": [],
            "card": card,
        }
    else:
        advance_turn(game)
    emit_state(room)


@socketio.on("play_ability")
def on_play_ability(data):
    room = data["room"].upper()
    game = rooms.get(room)
    if not game or not ensure_turn(game):
        return
    if not ensure_no_pending_burn(game):
        return
    ability = game.get("pending_ability")
    if (
        game["phase"] != "ability"
        or not ability
        or ability["sid"] != request.sid
        or ability["stage"] != "waiting"
    ):
        emit_error("No special card is waiting.")
        return
    ability["stage"] = "selecting"
    ability["selected"] = []
    emit_state(room)


@socketio.on("end_turn")
def on_end_turn(data):
    # Turns auto-advance after swap/play; kept for older clients as a no-op.
    room = data["room"].upper()
    game = rooms.get(room)
    if not game:
        return
    emit_error("Turns advance automatically after you play or swap.")


@socketio.on("ability_select_card")
def on_ability_select_card(data):
    room = data["room"].upper()
    game = rooms.get(room)
    if not game or not ensure_turn(game):
        return
    if not ensure_no_pending_burn(game):
        return
    ability = game["pending_ability"]
    if game["phase"] != "ability" or not ability or ability["sid"] != request.sid:
        emit_error("No ability is waiting.")
        return
    if ability["stage"] == "waiting":
        ability["stage"] = "selecting"
        ability["selected"] = []
    if ability["stage"] != "selecting":
        emit_error("That special is not selecting cards.")
        return

    owner_sid = data.get("owner_sid")
    index = int(data.get("index", -1))
    slot = slot_at(game, owner_sid, index)
    if not slot or not slot.get("card"):
        emit_error("Choose a live board card.")
        return

    ability_type = ability["type"]
    if ability_type == "peek_own" and owner_sid != request.sid:
        emit_error("That card only lets you peek at your own card.")
        return
    if ability_type == "peek_other" and owner_sid == request.sid:
        emit_error("That card only lets you peek at someone else's card.")
        return
    if ability_type in {"switch_unseen", "switch_peek"}:
        if protected_from_switch(game, request.sid, owner_sid):
            emit_error("That player called, so their cards are protected from switches.")
            return
        selected = ability["selected"]
        candidate = {"owner_sid": owner_sid, "index": index}
        if candidate in selected:
            emit_error("Choose two different card slots.")
            return
        selected.append(candidate)
        if len(selected) < 2:
            emit_state(room)
            return

        first = slot_at(game, selected[0]["owner_sid"], selected[0]["index"])
        second = slot_at(game, selected[1]["owner_sid"], selected[1]["index"])
        if not first or not second or not first.get("card") or not second.get("card"):
            game["pending_ability"] = None
            advance_turn(game)
            emit_state(room)
            return

        if ability_type == "switch_unseen":
            board_a = game["players"][selected[0]["owner_sid"]]["board"]
            board_b = game["players"][selected[1]["owner_sid"]]["board"]
            board_a[selected[0]["index"]], board_b[selected[1]["index"]] = (
                board_b[selected[1]["index"]],
                board_a[selected[0]["index"]],
            )
            set_last_action(
                game,
                "switch",
                sid=request.sid,
                a=selected[0],
                b=selected[1],
            )
            add_log(game, f"{player_name(game, request.sid)} switched two unseen cards.")
            game["pending_ability"] = None
            advance_turn(game)
        else:
            ability["stage"] = "deciding"
            ability["peek_pair"] = [
                {
                    "owner_sid": selected[0]["owner_sid"],
                    "index": selected[0]["index"],
                    "card": public_card(first["card"]),
                    "burnable": bool(
                        game["discard_pile"]
                        and burn_matches(game["discard_pile"][-1], first["card"])
                        and not is_slot_burnt(game, selected[0]["owner_sid"], selected[0]["index"])
                    ),
                },
                {
                    "owner_sid": selected[1]["owner_sid"],
                    "index": selected[1]["index"],
                    "card": public_card(second["card"]),
                    "burnable": bool(
                        game["discard_pile"]
                        and burn_matches(game["discard_pile"][-1], second["card"])
                        and not is_slot_burnt(game, selected[1]["owner_sid"], selected[1]["index"])
                    ),
                },
            ]
        emit_state(room)
        return

    begin_held_peek(game, request.sid, owner_sid, index)
    emit_state(room)


@socketio.on("ability_put_back")
def on_ability_put_back(data):
    room = data["room"].upper()
    game = rooms.get(room)
    if not game or game["status"] != "playing":
        return
    peek = game.get("held_peek")
    if not peek or peek["sid"] != request.sid:
        emit_error("You are not holding a peeked card.")
        return
    put_back_held_peek(game)
    emit_state(room)


@socketio.on("burn_from_peek")
def on_burn_from_peek(data):
    room = data["room"].upper()
    game = rooms.get(room)
    if not game or game["status"] != "playing":
        return
    peek = game.get("held_peek")
    if not peek or peek["sid"] != request.sid:
        emit_error("You are not holding a peeked card.")
        return
    if not game["discard_pile"]:
        emit_error("There is no discard to burn against.")
        return
    if is_slot_burnt(game, peek["owner_sid"], peek["index"]):
        apply_failed_burn(game, request.sid, peek["owner_sid"], peek["index"], peek["card"], "already_burnt")
        # Restore slot so reveal is visible, then clear held
        game["players"][peek["owner_sid"]]["board"][peek["index"]] = {
            "card": peek["card"],
            "revealed": True,
        }
        game["held_peek"] = None
        game["pending_ability"] = None
        advance_turn(game)
        emit_state(room)
        return

    if not burn_matches(game["discard_pile"][-1], peek["card"]):
        apply_failed_burn(game, request.sid, peek["owner_sid"], peek["index"], peek["card"], "rank")
        game["players"][peek["owner_sid"]]["board"][peek["index"]] = {
            "card": peek["card"],
            "revealed": True,
        }
        game["held_peek"] = None
        game["pending_ability"] = None
        advance_turn(game)
        emit_state(room)
        return

    owner_sid = peek["owner_sid"]
    index = peek["index"]
    target_card = peek["card"]
    game["held_peek"] = None
    game["pending_ability"] = None

    if owner_sid == request.sid:
        # Card already removed from board while held — just mark burnt.
        mark_slot_burnt(game, owner_sid, index)
        set_last_action(
            game,
            "burn",
            sid=request.sid,
            owner_sid=owner_sid,
            index=index,
            card=public_card(target_card),
            own=True,
            from_peek=True,
        )
        add_log(game, f"{player_name(game, request.sid)} burned their peeked {target_card['label']}.")
        advance_turn(game)
        emit_state(room)
        return

    # Opponent peek burn: put card back face-up then start give flow.
    game["players"][owner_sid]["board"][index] = {"card": target_card, "revealed": True}
    ok, err = apply_successful_opponent_burn(game, request.sid, owner_sid, index, target_card)
    if not ok:
        emit_error(err)
        emit_state(room)
        return
    # After give completes, turn should advance — finish_burn_give will not auto-advance.
    # Mark that ability finished via peek burn.
    game["_advance_after_burn_give"] = True
    emit_state(room)


@socketio.on("black_king_decision")
def on_black_king_decision(data):
    room = data["room"].upper()
    game = rooms.get(room)
    if not game or not ensure_turn(game):
        return
    if not ensure_no_pending_burn(game):
        return
    ability = game["pending_ability"]
    if (
        game["phase"] != "ability"
        or not ability
        or ability["sid"] != request.sid
        or ability["type"] != "switch_peek"
        or ability["stage"] != "deciding"
    ):
        emit_error("No black king decision is waiting.")
        return

    if data.get("switch"):
        selected = ability["selected"]
        first = slot_at(game, selected[0]["owner_sid"], selected[0]["index"])
        second = slot_at(game, selected[1]["owner_sid"], selected[1]["index"])
        if first and second and first.get("card") and second.get("card"):
            board_a = game["players"][selected[0]["owner_sid"]]["board"]
            board_b = game["players"][selected[1]["owner_sid"]]["board"]
            board_a[selected[0]["index"]], board_b[selected[1]["index"]] = (
                board_b[selected[1]["index"]],
                board_a[selected[0]["index"]],
            )
            set_last_action(
                game,
                "switch",
                sid=request.sid,
                a=selected[0],
                b=selected[1],
            )
            add_log(game, f"{player_name(game, request.sid)} looked and switched two cards.")
    else:
        add_log(game, f"{player_name(game, request.sid)} looked and kept the cards in place.")

    game["pending_ability"] = None
    advance_turn(game)
    emit_state(room)


@socketio.on("finish_ability")
def on_finish_ability(data):
    room = data["room"].upper()
    game = rooms.get(room)
    if not game or not ensure_turn(game):
        return
    if not ensure_no_pending_burn(game):
        return
    # Prefer put-back for held peeks.
    if game.get("held_peek") and game["held_peek"]["sid"] == request.sid:
        put_back_held_peek(game)
        emit_state(room)
        return
    ability = game["pending_ability"]
    if not ability or ability["sid"] != request.sid:
        return
    game["pending_ability"] = None
    advance_turn(game)
    emit_state(room)


@socketio.on("skip_ability")
def on_skip_ability(data):
    room = data["room"].upper()
    game = rooms.get(room)
    if not game or not ensure_turn(game):
        return
    if not ensure_no_pending_burn(game):
        return
    ability = game["pending_ability"]
    if not ability or ability["sid"] != request.sid:
        return
    if game.get("held_peek") and game["held_peek"]["sid"] == request.sid:
        put_back_held_peek(game)
        emit_state(room)
        return
    add_log(game, f"{player_name(game, request.sid)} skipped the ability.")
    game["pending_ability"] = None
    advance_turn(game)
    emit_state(room)


@socketio.on("call_round")
def on_call_round(data):
    room = data["room"].upper()
    game = rooms.get(room)
    if not game or not ensure_turn(game):
        return
    if not ensure_no_pending_burn(game):
        return
    if game["phase"] != "choose":
        emit_error("You can only call before drawing.")
        return

    mark_turn_started(game)
    player = game["players"][request.sid]
    player["called"] = True
    player["protected"] = True
    set_last_action(game, "call", sid=request.sid)
    if not game["first_caller_sid"]:
        game["first_caller_sid"] = request.sid
        game["final_turns_remaining"] = [sid for sid in game["player_order"] if sid != request.sid]
        add_log(game, f"{player_name(game, request.sid)} called. Everyone else gets one final turn.")
    else:
        add_log(game, f"{player_name(game, request.sid)} called to protect their cards.")
    advance_turn(game)
    emit_state(room)


@socketio.on("burn_card")
def on_burn_card(data):
    room = data["room"].upper()
    game = rooms.get(room)
    if not game:
        return
    ok, err = can_attempt_burn(game, request.sid)
    if not ok:
        emit_error(err)
        return

    owner_sid = data.get("owner_sid")
    index = int(data.get("index", -1))
    slot = slot_at(game, owner_sid, index)
    if not slot or not slot.get("card"):
        emit_error("Choose a live board card.")
        return

    top_card = game["discard_pile"][-1]
    target_card = slot["card"]

    if is_slot_burnt(game, owner_sid, index):
        apply_failed_burn(game, request.sid, owner_sid, index, target_card, "already_burnt")
        emit_state(room)
        return

    if is_burn_blocked(game, owner_sid, index, target_card["id"]):
        emit_error("You cannot burn a card you just took from discard.")
        return

    slot["revealed"] = True

    if not burn_matches(top_card, target_card):
        apply_failed_burn(game, request.sid, owner_sid, index, target_card, "rank")
        emit_state(room)
        return

    if owner_sid == request.sid:
        apply_successful_own_burn(game, request.sid, owner_sid, index, target_card)
        emit_state(room)
        return

    ok, err = apply_successful_opponent_burn(game, request.sid, owner_sid, index, target_card)
    if not ok:
        emit_error(err)
        emit_state(room)
        return
    emit_state(room)


@socketio.on("finish_burn_give")
def on_finish_burn_give(data):
    room = data["room"].upper()
    game = rooms.get(room)
    if not game or game["status"] != "playing" or not game["pending_burn"]:
        return
    pending = game["pending_burn"]
    if pending["sid"] != request.sid:
        emit_error("That burn is waiting for another player.")
        return

    give_index = int(data.get("index", -1))
    give_slot = slot_at(game, request.sid, give_index)
    target_slot = slot_at(game, pending["target_sid"], pending["target_index"])
    if not give_slot or not give_slot.get("card"):
        emit_error("Choose one of your live cards to give.")
        return
    if (
        not target_slot
        or not target_slot.get("card")
        or target_slot["card"]["id"] != pending["target_card_id"]
    ):
        game["pending_burn"] = None
        emit_error("That burn target changed.")
        emit_state(room)
        return

    given_label = give_slot["card"]["label"]
    burned_label = target_slot["card"]["label"]
    game["players"][pending["target_sid"]]["board"][pending["target_index"]] = {
        "card": give_slot["card"],
        "revealed": False,
    }
    game["players"][request.sid]["board"][give_index] = None
    game["pending_burn"] = None
    set_last_action(
        game,
        "burn_give",
        sid=request.sid,
        target_sid=pending["target_sid"],
        target_index=pending["target_index"],
        give_index=give_index,
    )
    add_log(
        game,
        f"{player_name(game, request.sid)} burned {burned_label} and gave {given_label} to {player_name(game, pending['target_sid'])}.",
    )
    if game.pop("_advance_after_burn_give", False):
        advance_turn(game)
    emit_state(room)


@socketio.on("disconnect")
def on_disconnect():
    room, game = find_room_by_sid(request.sid)
    if not game:
        return

    if game["status"] == "lobby":
        del game["players"][request.sid]
        game["player_order"] = [sid for sid in game["player_order"] if sid != request.sid]
        humans = live_human_sids(game)
        if request.sid == game["host_sid"] and humans:
            game["host_sid"] = humans[0]
        if not humans:
            rooms.pop(room, None)
            return
    else:
        game["players"][request.sid]["connected"] = False
        add_log(game, f"{player_name(game, request.sid)} disconnected.")

    emit_state(room)
