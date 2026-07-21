import random
import time


SUITS = {
    "clubs": {"short": "C", "symbol": "♣", "color": "black"},
    "spades": {"short": "S", "symbol": "♠", "color": "black"},
    "hearts": {"short": "H", "symbol": "♥", "color": "red"},
    "diamonds": {"short": "D", "symbol": "♦", "color": "red"},
}

RANKS = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]
BOARD_SIZE = 4
BOTTOM_ROW = {2, 3}
MIN_GRID_SIZE = 2
MAX_GRID_SIZE = 4
PEEK_MODES = {"none", "self", "all_opponents", "seat_opponent"}
WIN_CONDITIONS = {"first_bust_lowest", "last_standing"}


def card_value(rank, color=None):
    if rank == "JOKER":
        return -2
    if rank == "A":
        return 1
    if rank in {"2", "3", "4", "5", "6", "7", "8", "9", "10"}:
        return int(rank)
    if rank == "J":
        return 11
    if rank == "Q":
        return 12
    if rank == "K":
        return -1 if color == "red" else 13
    return 0


def card_ability(card):
    rank = card["rank"]
    if rank in {"7", "8"}:
        return "peek_own"
    if rank in {"9", "10"}:
        return "peek_other"
    if rank in {"J", "Q"}:
        return "switch_unseen"
    if rank == "K" and card["color"] == "black":
        return "switch_peek"
    return None


def make_card(rank, suit=None, deck_number=1, joker_number=None, joker_value=-2):
    if rank == "JOKER":
        card_id = f"D{deck_number}-JOKER-{joker_number}"
        return {
            "id": card_id,
            "rank": "JOKER",
            "suit": "joker",
            "suit_short": "",
            "suit_symbol": "★",
            "color": "joker",
            "label": "Joker",
            "short": "Joker",
            "face": "Joker",
            "value": joker_value,
            "burn_key": "JOKER",
            "ability": None,
        }

    suit_info = SUITS[suit]
    value = card_value(rank, suit_info["color"])
    face = f"{rank}{suit_info['symbol']}"
    card = {
        "id": f"D{deck_number}-{rank}-{suit_info['short']}",
        "rank": rank,
        "suit": suit,
        "suit_short": suit_info["short"],
        "suit_symbol": suit_info["symbol"],
        "color": suit_info["color"],
        "label": face,
        "short": face,
        "face": face,
        "value": value,
        "burn_key": rank,
        "ability": None,
    }
    card["ability"] = card_ability(card)
    return card


def build_deck(deck_count=1, jokers=2, joker_value=-2):
    deck = []
    for deck_number in range(1, deck_count + 1):
        for suit in SUITS:
            for rank in RANKS:
                deck.append(make_card(rank, suit=suit, deck_number=deck_number))
        for joker_number in range(1, jokers + 1):
            deck.append(
                make_card(
                    "JOKER",
                    deck_number=deck_number,
                    joker_number=joker_number,
                    joker_value=joker_value,
                )
            )
    random.shuffle(deck)
    return deck


def make_slot(card):
    return {"card": card, "revealed": False}


def empty_board(board_size=BOARD_SIZE):
    return [None for _ in range(board_size)]


def deal_board(deck, board_size=BOARD_SIZE):
    return [make_slot(deck.pop()) for _ in range(board_size)]


def public_card(card):
    if not card:
        return None
    return {
        "id": card["id"],
        "rank": card["rank"],
        "suit": card["suit"],
        "suit_short": card["suit_short"],
        "suit_symbol": card.get("suit_symbol", ""),
        "color": card["color"],
        "label": card["label"],
        "short": card["short"],
        "face": card.get("face", card["short"]),
        "value": card["value"],
        "ability": card["ability"],
        "burn_key": card["burn_key"],
    }


def score_board(board):
    total = 0
    for slot in board:
        if slot and slot.get("card"):
            total += slot["card"]["value"]
    return total


def card_count(board):
    return sum(1 for slot in board if slot and slot.get("card"))


def burn_matches(discard_card, target_card):
    if not discard_card or not target_card:
        return False
    return discard_card["burn_key"] == target_card["burn_key"]


def ability_label(ability):
    labels = {
        "peek_own": "Peek at one of your cards",
        "peek_other": "Peek at someone else's card",
        "switch_unseen": "Switch any two board cards unseen",
        "switch_peek": "Look at two cards, then choose whether to switch",
    }
    return labels.get(ability, "")


# Shared room, board, and discard helpers used by both human and bot modes.

def default_settings():
    return {
        "preset": "default",
        "target_score": 50,
        "win_condition": "last_standing",
        "grid_rows": 2,
        "grid_cols": 2,
        "grid_peek_modes": ["none", "none", "self", "self"],
        "opponent_peek_distance": 1,
        "opponent_peek_direction": "left",
        "joker_value": -2,
        "deck_count": 1,
        "jokers": 2,
    }


def clamp_grid_dimension(value, fallback=2):
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = fallback
    return max(MIN_GRID_SIZE, min(MAX_GRID_SIZE, value))


def normalized_grid_modes(raw_modes, rows, cols):
    size = rows * cols
    modes = list(raw_modes) if isinstance(raw_modes, (list, tuple)) else []
    return [
        modes[index] if index < len(modes) and modes[index] in PEEK_MODES else "none"
        for index in range(size)
    ]


def board_size_from_settings(settings):
    rows = clamp_grid_dimension(settings.get("grid_rows", 2))
    cols = clamp_grid_dimension(settings.get("grid_cols", 2))
    return rows * cols


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
        "final_countdown_token": 0,
        "final_countdown_deadline": None,
        "next_start_sid": None,
        "round_results": None,
        "winner_summary": None,
        "action_log": [],
        "bot_mode": False,
        "bot_match_log": [],
        "bot_scheduled_key": None,
        "bot_burn_checked": set(),
        "bot_burn_pending": set(),
        "burn_knowledge_epoch": 0,
        "discard_epoch": 0,
        "burn_window_started_at": None,
        "burn_window_card_id": None,
        "burn_contests": {},
        "burn_showdown": None,
        "burn_showdown_sequence": 0,
        "burn_locked_discard_ids": set(),
        "burnt_slots": [],
        "burn_blockers": [],
        "failed_burn_reveals": [],
        "action_sequence": 0,
        "last_action": None,
    }


def is_bot_player(player):
    return player.get("is_bot", False)


def make_player(username, is_bot=False, difficulty=None, bot_policy=None):
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
        "bot_policy": bot_policy or {},
        "bot_known_cards": {},
        "bot_telemetry": {
            "decisions": 0,
            "rounds": [],
            "events": [],
        },
        "eliminated": False,
        "spectating": False,
        "eliminated_round": None,
    }


def player_name(game, sid):
    player = game["players"].get(sid)
    return player["username"] if player else "Player"


def add_log(game, message):
    game["action_log"].append(message)
    game["action_log"] = game["action_log"][-8:]


def slot_key(owner_sid, index):
    return f"{owner_sid}:{index}"


def reset_discard_burn_state(game):
    game["discard_epoch"] = game.get("discard_epoch", 0) + 1
    game["burnt_slots"] = []
    game["burn_blockers"] = []
    game["bot_burn_checked"] = set()
    game["bot_burn_pending"] = set()
    game["burn_knowledge_epoch"] = 0
    game["burn_window_started_at"] = time.time()
    game["burn_window_card_id"] = (
        game["discard_pile"][-1]["id"] if game.get("discard_pile") else None
    )


def burn_locked_discard_ids(game):
    return game.setdefault("burn_locked_discard_ids", set())


def is_discard_burn_locked(game, card=None):
    if card is None:
        if not game.get("discard_pile"):
            return False
        card = game["discard_pile"][-1]
    return card["id"] in burn_locked_discard_ids(game)


def lock_discard_card_for_burn(game, card):
    if card:
        burn_locked_discard_ids(game).add(card["id"])


def unlock_discard_card_for_burn(game, card):
    if card:
        burn_locked_discard_ids(game).discard(card["id"])


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
    if not game["draw_pile"]:
        if len(game["discard_pile"]) > 1:
            recycled = game["discard_pile"][:-1]
            for recycled_card in recycled:
                unlock_discard_card_for_burn(game, recycled_card)
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
        board.append(make_slot(card))
        index = len(board) - 1
    return {
        "index": index,
        "card": public_card(card),
        "replaced": None,
        "expanded": empty is None,
    }


def discard_card(game, card, reset_burns=True, burn_locked=False):
    unlock_discard_card_for_burn(game, card)
    game["discard_pile"].append(card)
    if burn_locked:
        lock_discard_card_for_burn(game, card)
    if reset_burns:
        reset_discard_burn_state(game)


def discard_burned_card(game, card):
    if game.get("discard_pile"):
        lock_discard_card_for_burn(game, game["discard_pile"][-1])
    discard_card(game, card, burn_locked=True)


def can_attempt_burn(game, burner_sid):
    if game["status"] != "playing":
        return False, "The round is not active."
    player = game["players"].get(burner_sid)
    if not player:
        return False, "You are not in this game."
    if player.get("called"):
        return False, "You already called and cannot take any more actions this round."
    if game.get("pending_burn"):
        return False, "Finish the current burn first."
    if not game["discard_pile"]:
        return False, "There is no discard to burn against."
    if is_discard_burn_locked(game):
        return False, "That discard has already had a card burned on it."
    pending = game.get("pending_draw")
    if pending and pending.get("sid") == burner_sid:
        return False, "You cannot burn while holding a card."
    return True, None


def current_sid(game):
    if not game["player_order"]:
        return None
    sid = game["player_order"][game["turn_index"] % len(game["player_order"])]
    player = game["players"].get(sid)
    return sid if player and not player.get("eliminated") else None


def live_player_sids(game):
    return [sid for sid in game["player_order"] if sid in game["players"]]


def active_player_sids(game):
    return [
        sid
        for sid in game["player_order"]
        if sid in game["players"] and not game["players"][sid].get("eliminated")
    ]


def seat_opponent_sid(game, viewer_sid):
    order = active_player_sids(game)
    if viewer_sid not in order or len(order) < 2:
        return None
    settings = game["settings"]
    distance = max(1, min(len(order) - 1, int(settings.get("opponent_peek_distance", 1))))
    direction = -1 if settings.get("opponent_peek_direction") == "left" else 1
    return order[(order.index(viewer_sid) + direction * distance) % len(order)]


def can_opening_peek_slot(game, viewer_sid, owner_sid, index):
    viewer = game["players"].get(viewer_sid)
    owner = game["players"].get(owner_sid)
    if (
        not viewer
        or not owner
        or viewer.get("eliminated")
        or owner.get("eliminated")
        or viewer.get("first_turn_started")
        or game.get("status") != "playing"
    ):
        return False
    modes = game["settings"].get("grid_peek_modes", [])
    if index < 0 or index >= len(modes):
        return False
    mode = modes[index]
    if mode == "self":
        return viewer_sid == owner_sid
    if mode == "all_opponents":
        return viewer_sid != owner_sid
    if mode == "seat_opponent":
        return owner_sid == seat_opponent_sid(game, viewer_sid)
    return False


def slot_at(game, owner_sid, index):
    if owner_sid not in game["players"]:
        return None
    if index < 0 or index >= len(game["players"][owner_sid]["board"]):
        return None
    return game["players"][owner_sid]["board"][index]


def protected_from_switch(game, actor_sid, owner_sid):
    return owner_sid != actor_sid and game["players"][owner_sid].get("protected", False)


def live_slots_for(game, sid):
    return [
        (index, slot)
        for index, slot in enumerate(game["players"][sid]["board"])
        if slot and slot.get("card")
    ]


def all_switchable_slots(game, actor_sid):
    slots = []
    for owner_sid in active_player_sids(game):
        if owner_sid not in game["players"]:
            continue
        if protected_from_switch(game, actor_sid, owner_sid):
            continue
        for index, slot in live_slots_for(game, owner_sid):
            slots.append((owner_sid, index, slot))
    return slots


def swap_slots(game, first_owner, first_index, second_owner, second_index):
    first_board = game["players"][first_owner]["board"]
    second_board = game["players"][second_owner]["board"]
    first_board[first_index], second_board[second_index] = (
        second_board[second_index],
        first_board[first_index],
    )


def mark_turn_started(game):
    sid = current_sid(game)
    if sid and sid in game["players"]:
        player = game["players"][sid]
        player["first_turn_started"] = True
        player["opening_peeked"] = set()
