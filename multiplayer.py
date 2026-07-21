from copy import deepcopy
import threading
import time

from flask import request
from flask_socketio import emit, join_room

from extensions import socketio
from bot import (
    add_bot,
    initialize_bot_round_knowledge,
    maybe_schedule_bot_work,
    remember_bot_card,
    record_bot_round_outcomes,
    set_bot_difficulty,
)
from game import (
    WIN_CONDITIONS,
    active_player_sids,
    add_burn_blocker,
    add_log,
    ability_label,
    build_deck,
    burn_matches,
    board_size_from_settings,
    can_opening_peek_slot,
    card_count,
    clamp_grid_dimension,
    clear_burn_blockers,
    current_sid,
    default_settings,
    madhouse_settings,
    deal_board,
    deal_penalty_card,
    discard_burned_card,
    discard_card,
    empty_board,
    is_burn_blocked,
    is_bot_player,
    is_discard_burn_locked,
    is_slot_burnt,
    live_player_sids,
    make_player,
    make_slot,
    mark_slot_burnt,
    mark_turn_started,
    new_room,
    normalized_grid_modes,
    player_name,
    protected_from_switch,
    public_card,
    reset_discard_burn_state,
    score_board,
    slot_at,
    swap_slots,
    unlock_discard_card_for_burn,
)


rooms = {}
FINAL_COUNTDOWN_SECONDS = 3.0
BURN_CONTEST_SECONDS = 0.85
MAX_PLAYERS = 6
LOBBY_RECONNECT_GRACE_SECONDS = 60
MAX_CHAT_MESSAGES = 100
MAX_CHAT_LENGTH = 240
CHAT_COOLDOWN_SECONDS = 0.4
sid_redirects = {}


def _replace_sid_string(value, old_sid, new_sid):
    if value == old_sid:
        return new_sid
    prefix = f"{old_sid}:"
    if value.startswith(prefix):
        return f"{new_sid}:{value[len(prefix):]}"
    return value


def replace_sid_references(value, old_sid, new_sid):
    """Replace a Socket.IO sid everywhere it can appear in live room state."""
    if isinstance(value, str):
        return _replace_sid_string(value, old_sid, new_sid)
    if isinstance(value, dict):
        return {
            replace_sid_references(key, old_sid, new_sid): replace_sid_references(
                item, old_sid, new_sid
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [replace_sid_references(item, old_sid, new_sid) for item in value]
    if isinstance(value, tuple):
        return tuple(replace_sid_references(item, old_sid, new_sid) for item in value)
    if isinstance(value, set):
        return {replace_sid_references(item, old_sid, new_sid) for item in value}
    return value


def apply_sid_redirects(value):
    for old_sid, new_sid in list(sid_redirects.items()):
        value = replace_sid_references(value, old_sid, new_sid)
    return value


def rebind_player_sid(room, game, old_sid, new_sid):
    updated = replace_sid_references(game, old_sid, new_sid)
    game.clear()
    game.update(updated)
    for original, destination in list(sid_redirects.items()):
        if destination == old_sid:
            sid_redirects[original] = new_sid
    sid_redirects[old_sid] = new_sid
    rooms[room] = game
    return game


def reconnecting_player_sid(game, reconnect_token):
    if not reconnect_token:
        return None
    return next(
        (
            sid
            for sid, player in game["players"].items()
            if not is_bot_player(player)
            and player.get("reconnect_token") == reconnect_token
        ),
        None,
    )


def cleanup_disconnected_lobby_player(room, sid, reconnect_token):
    socketio.sleep(LOBBY_RECONNECT_GRACE_SECONDS)
    game = rooms.get(room)
    if not game or game.get("status") != "lobby":
        return
    player = game["players"].get(sid)
    if (
        not player
        or player.get("connected")
        or player.get("reconnect_token") != reconnect_token
    ):
        return
    del game["players"][sid]
    game["player_order"] = [item for item in game["player_order"] if item != sid]
    humans = live_human_sids(game)
    if sid == game["host_sid"] and humans:
        game["host_sid"] = humans[0]
    if not humans:
        rooms.pop(room, None)
        return
    emit_state(room)

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


def set_last_action(game, action_type, **payload):
    game["action_sequence"] = game.get("action_sequence", 0) + 1
    game["last_action"] = {
        "type": action_type,
        "id": game["action_sequence"],
        "epoch": game.get("discard_epoch", 0),
        **payload,
    }
    refresh_final_countdown(game)


def clear_last_action(game):
    game["last_action"] = None


def public_burnt_slots(game):
    result = []
    for key in game.get("burnt_slots", []):
        owner_sid, index = key.rsplit(":", 1)
        result.append({"owner_sid": owner_sid, "index": int(index)})
    return result


def visible_slot(game, viewer_sid, owner_sid, index, slot):
    if not slot or not slot.get("card"):
        return {"empty": True, "faceUp": True, "card": None}

    viewer = game["players"].get(viewer_sid, {})
    opening = viewer.get("opening_peeked") or set()
    opening_key = f"{owner_sid}:{index}"
    should_show = (
        game["status"] in {"round_over", "game_over"}
        or slot.get("revealed", False)
        or (
            not viewer.get("first_turn_started")
            and opening_key in opening
        )
    )
    if should_show:
        return {
            "empty": False,
            "faceUp": True,
            "card": public_card(slot["card"]),
            "openingPeekable": can_opening_peek_slot(
                game, viewer_sid, owner_sid, index
            ),
        }
    return {
        "empty": False,
        "faceUp": False,
        "card": None,
        "openingPeekable": can_opening_peek_slot(
            game, viewer_sid, owner_sid, index
        ),
    }


def has_opening_peek_available(game, viewer_sid):
    if viewer_sid not in game["players"]:
        return False
    return any(
        can_opening_peek_slot(game, viewer_sid, owner_sid, index)
        for owner_sid in active_player_sids(game)
        for index in range(len(game["players"][owner_sid]["board"]))
    )


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
            "eliminated": player.get("eliminated", False),
            "spectating": player.get("spectating", False),
            "eliminated_round": player.get("eliminated_round"),
            "first_turn_started": player["first_turn_started"],
            "opening_peekable": (
                sid == viewer_sid
                and has_opening_peek_available(game, viewer_sid)
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
            "targets": deepcopy(
                ability.get("inspected", ability.get("selected", []))
            ) if ability.get("type") == "switch_peek" else [],
        }
        if ability["sid"] == viewer_sid:
            pending_ability["inspection_count"] = ability.get(
                "inspection_count",
                len(ability.get("inspected", ability.get("selected", []))),
            )
            pending_ability["inspected"] = deepcopy(ability.get("inspected", []))
            pending_ability["burned_selection"] = bool(ability.get("burned_selection"))
            pending_ability["burned_cards"] = deepcopy(ability.get("burned_cards", []))
            pending_ability["moved_cards"] = deepcopy(ability.get("moved_cards", []))
            pending_ability["can_switch"] = bool(ability.get("can_switch"))
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

    countdown_deadline = game.get("final_countdown_deadline")
    countdown_ends_at = None
    if countdown_deadline is not None:
        countdown_ends_at = int(countdown_deadline * 1000)

    return {
        "status": game["status"],
        "settings": game["settings"],
        "round_number": game["round_number"],
        "players": players,
        "player_order": live_player_sids(game),
        "active_player_order": active_player_sids(game),
        "host_sid": game["host_sid"],
        "viewer_sid": viewer_sid,
        "current_turn_sid": current_sid(game),
        "phase": game["phase"],
        "draw_count": len(game["draw_pile"]),
        "discard_count": len(game["discard_pile"]),
        "discard_top": public_card(game["discard_pile"][-1]) if game["discard_pile"] else None,
        "discard_burn_available": bool(
            game["discard_pile"] and not is_discard_burn_locked(game)
        ),
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
        "final_countdown_ends_at": countdown_ends_at,
        "round_results": deepcopy(game["round_results"]),
        "winner_summary": deepcopy(game["winner_summary"]),
        "burn_showdown": deepcopy(game.get("burn_showdown")),
        "action_log": list(game["action_log"]),
    }


def emit_state(room):
    game = rooms.get(room)
    if not game:
        return
    for sid in live_human_sids(game):
        socketio.emit("game_state", player_view(game, sid), room=sid)
    maybe_schedule_bot_work(room)


def final_countdown_pending(game):
    return bool(
        game.get("pending_draw")
        or game.get("pending_ability")
        or game.get("pending_burn")
        or game.get("held_peek")
        or game.get("active_burn_contest_id")
    )


def pause_final_countdown(game):
    game["final_countdown_token"] = game.get("final_countdown_token", 0) + 1
    game["final_countdown_deadline"] = None


def refresh_final_countdown(game):
    if (
        game.get("status") != "playing"
        or not game.get("first_caller_sid")
        or game.get("final_turns_remaining")
    ):
        return
    if final_countdown_pending(game):
        pause_final_countdown(game)
        return

    room = game.get("room_id")
    if not room:
        return
    game["phase"] = "final_countdown"
    game["final_countdown_token"] = game.get("final_countdown_token", 0) + 1
    token = game["final_countdown_token"]
    game["final_countdown_deadline"] = time.time() + FINAL_COUNTDOWN_SECONDS
    socketio.start_background_task(run_final_countdown, room, token)


def run_final_countdown(room, token):
    socketio.sleep(FINAL_COUNTDOWN_SECONDS)
    game = rooms.get(room)
    if (
        not game
        or game.get("status") != "playing"
        or game.get("final_countdown_token") != token
        or game.get("final_turns_remaining")
        or final_countdown_pending(game)
    ):
        return
    game["final_countdown_deadline"] = None
    finish_round(game)
    emit_state(room)


def emit_error(message):
    emit("error_message", {"msg": message}, room=request.sid)


def chat_history(game):
    return deepcopy(game.get("chat_messages", []))[-MAX_CHAT_MESSAGES:]


def start_round(game):
    active_sids = active_player_sids(game)
    board_size = board_size_from_settings(game["settings"])
    jokers = int(game["settings"].get("jokers", 2))
    deck_count = max(1, min(4, int(game["settings"].get("deck_count", 1))))
    deck = build_deck(
        deck_count=deck_count,
        jokers=jokers,
        joker_value=int(game["settings"].get("joker_value", -2)),
    )
    for sid in game["player_order"]:
        player = game["players"][sid]
        player["board"] = (
            deal_board(deck, board_size)
            if sid in active_sids
            else empty_board(board_size)
        )
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
    pause_final_countdown(game)
    game["round_results"] = None
    game["winner_summary"] = None
    game["action_log"] = []
    game["discard_epoch"] = 0
    game["burn_window_started_at"] = None
    game["burn_window_card_id"] = None
    game["burn_contests"] = {}
    game["active_burn_contest_id"] = None
    game["burn_showdown"] = None
    game["burn_locked_discard_ids"] = set()
    game["burnt_slots"] = []
    game["burn_blockers"] = []
    game["failed_burn_reveals"] = []
    game["last_action"] = None
    game["bot_burn_checked"] = set()
    game["bot_burn_pending"] = set()
    game["burn_knowledge_epoch"] = 0

    if game["next_start_sid"] in active_sids:
        game["turn_index"] = game["player_order"].index(game["next_start_sid"])
    else:
        game["turn_index"] = game["player_order"].index(active_sids[0]) if active_sids else 0

    game["status"] = "playing"
    initialize_bot_round_knowledge(game)
    # Each player keeps their opening peek until they take their own first action.
    add_log(game, f"Round {game['round_number']} started. {player_name(game, current_sid(game))} goes first.")


def expire_failed_burn_reveals(game):
    remaining = []
    for reveal in game.get("failed_burn_reveals", []):
        reveal["turns_remaining"] -= 1
        if reveal["turns_remaining"] > 0:
            remaining.append(reveal)
            continue
        for player in game["players"].values():
            for slot in player["board"]:
                if (
                    slot
                    and slot.get("card")
                    and slot["card"]["id"] == reveal["card_id"]
                ):
                    slot["revealed"] = False
    game["failed_burn_reveals"] = remaining


def track_failed_burn_reveal(game, target_card):
    reveals = game.setdefault("failed_burn_reveals", [])
    for reveal in reveals:
        if reveal["card_id"] == target_card["id"]:
            reveal["turns_remaining"] = 2
            return
    reveals.append({"card_id": target_card["id"], "turns_remaining": 2})


def advance_turn(game):
    # A missed burn stays visible for the rest of this turn and all of the next.
    expire_failed_burn_reveals(game)
    previous_sid = current_sid(game)
    if previous_sid in game["final_turns_remaining"]:
        game["final_turns_remaining"].remove(previous_sid)

    if game["first_caller_sid"] and not game["final_turns_remaining"]:
        refresh_final_countdown(game)
        return

    active_sids = active_player_sids(game)
    if not active_sids:
        return

    for _ in range(len(game["player_order"])):
        game["turn_index"] = (game["turn_index"] + 1) % len(game["player_order"])
        next_sid = current_sid(game)
        if next_sid not in active_sids:
            continue
        if game["first_caller_sid"] and next_sid not in game["final_turns_remaining"]:
            continue
        break

    game["phase"] = "choose"
    game["pending_draw"] = None
    game["pending_ability"] = None
    game["held_peek"] = None


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
    round_sids = active_player_sids(game)
    hand_scores = {
        sid: score_board(game["players"][sid]["board"])
        for sid in round_sids
    }
    if not hand_scores:
        return
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
    record_bot_round_outcomes(game, hand_scores, round_scores)

    target_score = int(game["settings"]["target_score"])
    over_target = [
        sid for sid in round_sids
        if game["players"][sid]["score"] >= target_score
    ]
    win_condition = game["settings"].get("win_condition", "last_standing")

    game["round_results"] = {
        "raw_scores": hand_scores,
        "round_scores": round_scores,
        "negative_bump": negative_bump,
        "doubled_caller": doubled_caller,
        "caller_sid": caller_sid,
        "next_start_sid": game["next_start_sid"],
        "over_target": over_target,
        "eliminated": [],
    }
    game["phase"] = "round_over"
    game["pending_draw"] = None
    game["pending_ability"] = None
    game["pending_burn"] = None
    game["held_peek"] = None
    pause_final_countdown(game)
    clear_burn_blockers(game)
    clear_last_action(game)

    if over_target and win_condition == "first_bust_lowest":
        lowest_total = min(game["players"][sid]["score"] for sid in round_sids)
        winners = [
            sid for sid in round_sids
            if game["players"][sid]["score"] == lowest_total
        ]
        game["status"] = "game_over"
        game["winner_summary"] = {
            "condition": win_condition,
            "winners": winners,
            "losers": over_target,
            "target_score": target_score,
        }
        add_log(game, f"Game over. {player_name(game, winners[0])} has the lowest score.")
        return

    if over_target and win_condition == "last_standing":
        for sid in over_target:
            player = game["players"][sid]
            player["eliminated"] = True
            player["spectating"] = True
            player["eliminated_round"] = game["round_number"]
        game["round_results"]["eliminated"] = list(over_target)
        survivors = active_player_sids(game)
        if len(survivors) <= 1:
            if survivors:
                winners = survivors
            else:
                lowest_total = min(game["players"][sid]["score"] for sid in round_sids)
                winners = [
                    sid for sid in round_sids
                    if game["players"][sid]["score"] == lowest_total
                ]
            game["status"] = "game_over"
            game["winner_summary"] = {
                "condition": win_condition,
                "winners": winners,
                "losers": [sid for sid in round_sids if sid not in winners],
                "target_score": target_score,
            }
            add_log(game, f"Game over. {player_name(game, winners[0])} is the last player standing.")
            return

        survivor_low = min(hand_scores[sid] for sid in survivors)
        game["next_start_sid"] = choose_low_tie_starter(
            game,
            [sid for sid in survivors if hand_scores[sid] == survivor_low],
        )
        game["round_results"]["next_start_sid"] = game["next_start_sid"]

    game["status"] = "round_over"
    add_log(game, f"Round ended. {player_name(game, game['next_start_sid'])} starts next.")


def ensure_turn(game):
    if game["status"] != "playing":
        emit_error("The round is not active.")
        return False
    player = game["players"].get(request.sid)
    if player and player.get("eliminated"):
        emit_error("You are spectating and cannot take game actions.")
        return False
    if player and player.get("called"):
        emit_error("You already called and cannot take any more actions this round.")
        return False
    if current_sid(game) != request.sid:
        emit_error("It is not your turn.")
        return False
    return True


def ensure_no_pending_burn(game):
    if game.get("pending_burn"):
        emit_error("Finish the burn first.")
        return False
    if game.get("active_burn_contest_id"):
        emit_error("A burn showdown is being decided.")
        return False
    return True


def refresh_switch_peek_cards(game, ability):
    peek_pair = []
    for candidate in ability.get("selected", []):
        slot = slot_at(game, candidate["owner_sid"], candidate["index"])
        if not slot or not slot.get("card"):
            continue
        card = slot["card"]
        peek_pair.append(
            {
                "owner_sid": candidate["owner_sid"],
                "index": candidate["index"],
                "card": public_card(card),
                "burnable": bool(
                    game["discard_pile"]
                    and not is_discard_burn_locked(game)
                    and burn_matches(game["discard_pile"][-1], card)
                    and not is_slot_burnt(
                        game,
                        candidate["owner_sid"],
                        candidate["index"],
                    )
                    and not is_burn_blocked(
                        game,
                        candidate["owner_sid"],
                        candidate["index"],
                        card["id"],
                    )
                ),
            }
        )
    ability["peek_pair"] = peek_pair
    ability["can_switch"] = bool(
        not ability.get("burned_selection")
        and ability.get("inspection_count", 0) >= 2
        and len(ability.get("selected", [])) == 2
    )


def record_switch_peek_burn(game, actor_sid, owner_sid, index, target_card):
    ability = game.get("pending_ability")
    if (
        not ability
        or ability.get("sid") != actor_sid
        or ability.get("type") != "switch_peek"
    ):
        return False
    candidate = {"owner_sid": owner_sid, "index": index}
    if candidate not in ability.get("selected", []):
        return False
    ability["selected"].remove(candidate)
    ability["burned_selection"] = True
    ability.setdefault("burned_cards", []).append(
        {
            "owner_sid": owner_sid,
            "index": index,
            "card": public_card(target_card),
        }
    )
    refresh_switch_peek_cards(game, ability)
    ability["stage"] = (
        "deciding"
        if ability.get("inspection_count", 0) >= 2
        else "selecting"
    )
    return True


def record_switch_peek_give(game, actor_sid, give_index, given_card):
    ability = game.get("pending_ability")
    if (
        not ability
        or ability.get("sid") != actor_sid
        or ability.get("type") != "switch_peek"
    ):
        return False
    candidate = {"owner_sid": actor_sid, "index": give_index}
    if candidate not in ability.get("selected", []):
        return False
    ability["selected"].remove(candidate)
    ability.setdefault("moved_cards", []).append(
        {
            "owner_sid": actor_sid,
            "index": give_index,
            "card": public_card(given_card),
        }
    )
    refresh_switch_peek_cards(game, ability)
    ability["stage"] = (
        "deciding"
        if ability.get("inspection_count", 0) >= 2
        else "selecting"
    )
    return True


def resolve_unseen_switch(room, actor_sid, selected):
    socketio.sleep(0.8)
    game = rooms.get(room)
    while game and game.get("active_burn_contest_id"):
        socketio.sleep(0.05)
        game = rooms.get(room)
    if not game:
        return
    actor_sid = apply_sid_redirects(actor_sid)
    selected = apply_sid_redirects(selected)
    ability = game.get("pending_ability")
    if (
        game.get("status") != "playing"
        or game.get("phase") != "ability"
        or not ability
        or ability.get("sid") != actor_sid
        or ability.get("type") != "switch_unseen"
        or ability.get("stage") != "switching"
        or ability.get("selected") != selected
    ):
        return

    first = slot_at(game, selected[0]["owner_sid"], selected[0]["index"])
    second = slot_at(game, selected[1]["owner_sid"], selected[1]["index"])
    if first and second and first.get("card") and second.get("card"):
        swap_slots(
            game,
            selected[0]["owner_sid"],
            selected[0]["index"],
            selected[1]["owner_sid"],
            selected[1]["index"],
        )
        set_last_action(
            game,
            "switch",
            sid=actor_sid,
            a=selected[0],
            b=selected[1],
        )
        add_log(game, f"{player_name(game, actor_sid)} switched two unseen cards.")
    game["pending_ability"] = None
    advance_turn(game)
    emit_state(room)


def apply_successful_own_burn(game, burner_sid, owner_sid, index, target_card):
    game["players"][owner_sid]["board"][index] = None
    discard_burned_card(game, target_card)
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
    game["players"][owner_sid]["board"][index] = None
    discard_burned_card(game, target_card)
    mark_slot_burnt(game, owner_sid, index)
    game["pending_burn"] = {
        "sid": burner_sid,
        "target_sid": owner_sid,
        "target_index": index,
        "target_card_id": target_card["id"],
        "target_card": target_card,
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
        track_failed_burn_reveal(game, target_card)
        game["burn_knowledge_epoch"] = game.get("burn_knowledge_epoch", 0) + 1
        for observer_sid, player in game["players"].items():
            if is_bot_player(player):
                remember_bot_card(
                    game,
                    observer_sid,
                    owner_sid,
                    index,
                    target_card,
                )
    penalty = deal_penalty_card(game, burner_sid)
    penalty_action = None
    if penalty:
        penalty_action = {
            "index": penalty["index"],
            "expanded": penalty["expanded"],
        }
    set_last_action(
        game,
        "burn_fail",
        sid=burner_sid,
        owner_sid=owner_sid,
        index=index,
        card=public_card(target_card),
        reason=reason,
        penalty=penalty_action,
    )
    penalty_note = ""
    if penalty:
        penalty_note = f" Penalty card dealt to slot {penalty['index'] + 1}."
    add_log(
        game,
        f"{player_name(game, burner_sid)} missed a burn on {player_name(game, owner_sid)}'s card.{penalty_note}",
    )


def burn_contest_for(game, discard_card_id, discard_card=None, attempted_at=None):
    contests = game.setdefault("burn_contests", {})
    contest = contests.get(discard_card_id)
    if contest:
        return contest
    if discard_card is None:
        return None
    attempted_at = attempted_at if attempted_at is not None else time.time()
    started_at = game.get("burn_window_started_at")
    if game.get("burn_window_card_id") != discard_card_id or started_at is None:
        started_at = attempted_at
    contest = {
        "discard_card_id": discard_card_id,
        "discard_card": discard_card,
        "started_at": started_at,
        "opened_at": attempted_at,
        "resolve_at": attempted_at + BURN_CONTEST_SECONDS,
        "resolution_scheduled": False,
        "resolved": False,
        "attempts": [],
        "winner_sid": None,
        "winner_target": None,
    }
    contests[discard_card_id] = contest
    if len(contests) > 12:
        oldest_id = next(iter(contests))
        if oldest_id != discard_card_id:
            contests.pop(oldest_id, None)
    return contest


def burn_lock_for(game):
    lock = game.get("_burn_contest_lock")
    if lock is None:
        lock = threading.RLock()
        game["_burn_contest_lock"] = lock
    return lock


def register_burn_attempt(
    game,
    contest,
    sid,
    owner_sid,
    index,
    target_card,
    attempted_at,
    inspection_burn=False,
):
    if any(attempt["sid"] == sid for attempt in contest["attempts"]):
        return None
    elapsed_ms = max(0, int(round((attempted_at - contest["started_at"]) * 1000)))
    attempt = {
        "sid": sid,
        "owner_sid": owner_sid,
        "index": index,
        "target_card": target_card,
        "matches": burn_matches(contest["discard_card"], target_card),
        "inspection_burn": bool(inspection_burn),
        "time_ms": elapsed_ms,
        "result": "pending",
        "penalty": False,
    }
    contest["attempts"].append(attempt)
    return attempt


def publish_burn_showdown(game, contest):
    if not contest.get("attempts"):
        return
    attempts = sorted(contest["attempts"], key=lambda item: item["time_ms"])
    baseline = next(
        (
            attempt["time_ms"]
            for attempt in attempts
            if attempt["sid"] == contest.get("winner_sid")
        ),
        attempts[0]["time_ms"],
    )
    public_attempts = [
        {
            "sid": attempt["sid"],
            "owner_sid": attempt["owner_sid"],
            "index": attempt["index"],
            "time_ms": attempt["time_ms"],
            "delta_ms": attempt["time_ms"] - baseline,
            "result": attempt["result"],
            "penalty": attempt["penalty"],
        }
        for attempt in attempts
    ]
    game["burn_showdown_sequence"] = game.get("burn_showdown_sequence", 0) + 1
    game["burn_showdown"] = {
        "id": game["burn_showdown_sequence"],
        "discard_card": public_card(contest["discard_card"]),
        "winner_sid": contest["winner_sid"],
        "winner_target": deepcopy(contest.get("winner_target")),
        "contest_window_ms": int(round(BURN_CONTEST_SECONDS * 1000)),
        "attempts": public_attempts,
    }


def burn_attempt_is_eligible(game, attempt):
    player = game["players"].get(attempt["sid"])
    if not player or player.get("eliminated") or player.get("called"):
        return False
    pending_draw = game.get("pending_draw")
    if pending_draw and pending_draw.get("sid") == attempt["sid"]:
        return False
    slot = slot_at(game, attempt["owner_sid"], attempt["index"])
    if not slot or not slot.get("card"):
        return False
    if slot["card"]["id"] != attempt["target_card"]["id"]:
        return False
    if is_slot_burnt(game, attempt["owner_sid"], attempt["index"]):
        return False
    if is_burn_blocked(
        game,
        attempt["owner_sid"],
        attempt["index"],
        attempt["target_card"]["id"],
    ):
        return False
    return not (
        attempt["owner_sid"] != attempt["sid"]
        and card_count(player["board"]) == 0
    )


def finalize_burn_contest(game, discard_card_id):
    with burn_lock_for(game):
        return finalize_burn_contest_locked(game, discard_card_id)


def finalize_burn_contest_locked(game, discard_card_id):
    contest = game.get("burn_contests", {}).get(discard_card_id)
    if not contest or contest.get("resolved"):
        return False
    contest["resolved"] = True
    attempts = sorted(contest["attempts"], key=lambda item: item["time_ms"])
    current_top = game["discard_pile"][-1] if game.get("discard_pile") else None
    discard_still_live = bool(
        current_top
        and current_top["id"] == discard_card_id
        and not is_discard_burn_locked(game, current_top)
        and not game.get("pending_burn")
    )
    winner = next(
        (
            attempt
            for attempt in attempts
            if discard_still_live
            and attempt["matches"]
            and burn_attempt_is_eligible(game, attempt)
        ),
        None,
    )

    for attempt in attempts:
        if attempt is winner:
            continue
        if not discard_still_live or not burn_attempt_is_eligible(game, attempt):
            attempt["result"] = "cancelled"
            continue
        reason = "race_lost" if winner and attempt["matches"] else "rank"
        apply_failed_burn(
            game,
            attempt["sid"],
            attempt["owner_sid"],
            attempt["index"],
            attempt["target_card"],
            reason,
        )
        attempt["result"] = "late" if winner and attempt["matches"] else "miss"
        attempt["penalty"] = True

    if winner:
        if winner["owner_sid"] == winner["sid"]:
            apply_successful_own_burn(
                game,
                winner["sid"],
                winner["owner_sid"],
                winner["index"],
                winner["target_card"],
            )
        else:
            ok, _ = apply_successful_opponent_burn(
                game,
                winner["sid"],
                winner["owner_sid"],
                winner["index"],
                winner["target_card"],
            )
            if not ok:
                winner = None
        if winner:
            winner["result"] = "winner"
            contest["winner_sid"] = winner["sid"]
            contest["winner_target"] = {
                "owner_sid": winner["owner_sid"],
                "index": winner["index"],
                "card": public_card(winner["target_card"]),
            }
            if winner.get("inspection_burn"):
                record_switch_peek_burn(
                    game,
                    winner["sid"],
                    winner["owner_sid"],
                    winner["index"],
                    winner["target_card"],
                )

    game["active_burn_contest_id"] = None
    publish_burn_showdown(game, contest)
    refresh_final_countdown(game)
    return True


def run_burn_contest_resolution(room, discard_card_id):
    game = rooms.get(room)
    contest = game.get("burn_contests", {}).get(discard_card_id) if game else None
    if not contest:
        return
    socketio.sleep(max(0, contest["resolve_at"] - time.time()))
    game = rooms.get(room)
    if not game:
        return
    if finalize_burn_contest(game, discard_card_id):
        emit_state(room)


def resolve_burn_attempt(
    game,
    burner_sid,
    owner_sid,
    index,
    discard_card_id=None,
    attempted_at=None,
    inspection_burn=False,
):
    with burn_lock_for(game):
        return resolve_burn_attempt_locked(
            game,
            burner_sid,
            owner_sid,
            index,
            discard_card_id,
            attempted_at,
            inspection_burn,
        )


def resolve_burn_attempt_locked(
    game,
    burner_sid,
    owner_sid,
    index,
    discard_card_id=None,
    attempted_at=None,
    inspection_burn=False,
):
    """Register a burn using the server clock; mutate only after the contest window."""
    attempted_at = attempted_at if attempted_at is not None else time.time()
    if game.get("status") != "playing":
        return "error", None, "The round is not active."
    player = game["players"].get(burner_sid)
    if not player:
        return "error", None, "You are not in this game."
    if player.get("eliminated"):
        return "error", None, "You are spectating and cannot attempt burns."
    if player.get("called"):
        return "error", None, "You already called and cannot take any more actions this round."
    pending_draw = game.get("pending_draw")
    if pending_draw and pending_draw.get("sid") == burner_sid:
        return "error", None, "You cannot burn while holding a card."

    current_top = game["discard_pile"][-1] if game.get("discard_pile") else None
    if discard_card_id is None and current_top:
        discard_card_id = current_top["id"]
    contest = game.get("burn_contests", {}).get(discard_card_id)
    normal_attempt = bool(
        current_top
        and current_top["id"] == discard_card_id
        and not is_discard_burn_locked(game, current_top)
        and not game.get("pending_burn")
    )
    if contest and contest.get("resolved") and normal_attempt:
        game["burn_contests"].pop(discard_card_id, None)
        contest = None
    if not normal_attempt:
        return "error", None, "That discard is no longer available to burn."
    if contest and attempted_at > contest["resolve_at"]:
        return "error", None, "The burn showdown window has closed."
    active_contest_id = game.get("active_burn_contest_id")
    if active_contest_id and active_contest_id != discard_card_id:
        return "error", None, "Another burn showdown is being decided."

    if contest is None:
        contest = burn_contest_for(
            game,
            discard_card_id,
            current_top,
            attempted_at,
        )
    slot = slot_at(game, owner_sid, index)
    target_card = slot.get("card") if slot else None
    if not target_card:
        return "error", None, "That burn target changed."
    if is_burn_blocked(game, owner_sid, index, target_card["id"]):
        return "error", target_card, "You cannot burn a card you just took from discard."
    if owner_sid != burner_sid and card_count(player["board"]) == 0:
        return "error", target_card, "You need one of your own cards to give them."

    attempt = register_burn_attempt(
        game,
        contest,
        burner_sid,
        owner_sid,
        index,
        target_card,
        attempted_at,
        inspection_burn,
    )
    if attempt is None:
        return "error", None, "You already attempted this burn."
    game["active_burn_contest_id"] = discard_card_id
    pause_final_countdown(game)
    room = game.get("room_id")
    if (
        room
        and rooms.get(room) is game
        and not contest.get("resolution_scheduled")
    ):
        contest["resolution_scheduled"] = True
        socketio.start_background_task(
            run_burn_contest_resolution,
            room,
            discard_card_id,
        )
    return "pending", target_card, None


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


def begin_held_peek(game, viewer_sid, owner_sid, index):
    slot = slot_at(game, owner_sid, index)
    if not slot or not slot.get("card"):
        return False
    card = slot["card"]
    was_revealed = slot.get("revealed", False)
    game["players"][owner_sid]["board"][index] = None
    burnable = bool(
        game["discard_pile"]
        and not is_discard_burn_locked(game)
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


@socketio.on("join")
def on_join(data):
    data = data or {}
    room = str(data.get("room", "")).upper().strip()
    if not room:
        emit_error("A room code is required.")
        return
    username = str(data.get("username", "Anonymous")).strip()[:32] or "Anonymous"
    reconnect_token = str(data.get("reconnect_token", "")).strip()[:128]

    if room not in rooms:
        rooms[room] = new_room(request.sid)

    game = rooms[room]
    game["room_id"] = room
    reconnect_sid = reconnecting_player_sid(game, reconnect_token)
    if reconnect_sid and reconnect_sid != request.sid:
        reconnecting_player = game["players"][reconnect_sid]
        if reconnecting_player.get("connected"):
            emit_error("This player is already connected in another tab.")
            return
        game = rebind_player_sid(room, game, reconnect_sid, request.sid)
        game["players"][request.sid]["username"] = username
        game["players"][request.sid]["connected"] = True
    elif request.sid not in game["players"]:
        if len(game["players"]) >= MAX_PLAYERS:
            emit_error("This room is full.")
            return
        game["players"][request.sid] = make_player(username)
        game["players"][request.sid]["reconnect_token"] = reconnect_token
        if game["status"] != "lobby":
            game["players"][request.sid]["eliminated"] = True
            game["players"][request.sid]["spectating"] = True
        game["player_order"].append(request.sid)
    else:
        game["players"][request.sid]["username"] = username
        game["players"][request.sid]["connected"] = True
        if reconnect_token:
            game["players"][request.sid]["reconnect_token"] = reconnect_token

    join_room(room)
    emit_state(room)
    emit("chat_history", {"messages": chat_history(game)}, room=request.sid)


@socketio.on("send_chat")
def on_send_chat(data):
    data = data or {}
    room = str(data.get("room", "")).upper().strip()
    game = rooms.get(room)
    player = game.get("players", {}).get(request.sid) if game else None
    if not player or is_bot_player(player) or not player.get("connected"):
        emit_error("Join the room before sending a message.")
        return

    message = " ".join(str(data.get("message", ""))[:1000].split())
    if not message:
        return
    message = message[:MAX_CHAT_LENGTH]

    now = time.time()
    if now - float(player.get("last_chat_at", 0.0)) < CHAT_COOLDOWN_SECONDS:
        emit_error("Please wait a moment before sending another message.")
        return
    player["last_chat_at"] = now
    game["chat_sequence"] = game.get("chat_sequence", 0) + 1
    chat_message = {
        "id": game["chat_sequence"],
        "sender_sid": request.sid,
        "username": player["username"],
        "message": message,
        "sent_at": int(now * 1000),
    }
    game.setdefault("chat_messages", []).append(chat_message)
    game["chat_messages"] = game["chat_messages"][-MAX_CHAT_MESSAGES:]
    socketio.emit("chat_message", deepcopy(chat_message), room=room)


@socketio.on("update_settings")
def on_update_settings(data):
    room = data["room"].upper()
    game = rooms.get(room)
    if not game or request.sid != game["host_sid"] or game["status"] != "lobby":
        return

    if data.get("preset") == "default":
        game["settings"] = default_settings()
        emit_state(room)
        return
    if data.get("preset") == "madhouse":
        game["settings"] = madhouse_settings()
        emit_state(room)
        return

    current = game["settings"]
    try:
        target_score = max(10, min(500, int(data.get("target_score", current["target_score"]))))
    except (TypeError, ValueError):
        target_score = current["target_score"]
    rows = clamp_grid_dimension(data.get("grid_rows", current.get("grid_rows", 2)))
    cols = clamp_grid_dimension(data.get("grid_cols", current.get("grid_cols", 2)))
    modes = normalized_grid_modes(
        data.get("grid_peek_modes", current.get("grid_peek_modes", [])),
        rows,
        cols,
    )
    win_condition = data.get("win_condition", current.get("win_condition"))
    if win_condition not in WIN_CONDITIONS:
        win_condition = "last_standing"
    direction = data.get(
        "opponent_peek_direction",
        current.get("opponent_peek_direction", "left"),
    )
    if direction not in {"left", "right"}:
        direction = "left"
    try:
        distance = max(
            1,
            min(
                MAX_PLAYERS - 1,
                int(data.get("opponent_peek_distance", current.get("opponent_peek_distance", 1))),
            ),
        )
    except (TypeError, ValueError):
        distance = 1
    try:
        joker_value = int(data.get("joker_value", current.get("joker_value", -2)))
    except (TypeError, ValueError):
        joker_value = -2
    if joker_value not in {-2, 0}:
        joker_value = -2
    try:
        deck_count = max(1, min(4, int(data.get("deck_count", current.get("deck_count", 1)))))
    except (TypeError, ValueError):
        deck_count = current.get("deck_count", 1)
    try:
        jokers = max(0, min(20, int(data.get("jokers", current.get("jokers", 2)))))
    except (TypeError, ValueError):
        jokers = current.get("jokers", 2)
    game["settings"].update(
        {
            "preset": "custom",
            "target_score": target_score,
            "win_condition": win_condition,
            "grid_rows": rows,
            "grid_cols": cols,
            "grid_peek_modes": modes,
            "opponent_peek_distance": distance,
            "opponent_peek_direction": direction,
            "deck_count": deck_count,
            "jokers": jokers,
            "joker_value": joker_value,
        }
    )
    emit_state(room)


@socketio.on("add_bot")
def on_add_bot(data):
    room = data["room"].upper()
    game = rooms.get(room)
    if not game or request.sid != game["host_sid"] or game["status"] != "lobby":
        return
    if len(game["players"]) >= MAX_PLAYERS:
        emit_error(f"Rooms currently support up to {MAX_PLAYERS} players and bots.")
        return
    add_bot(game, room, data.get("difficulty", "medium"))
    emit_state(room)


@socketio.on("remove_bot")
def on_remove_bot(data):
    room = data["room"].upper()
    game = rooms.get(room)
    sid = data.get("sid")
    if not game or request.sid != game["host_sid"] or game["status"] != "lobby":
        return
    player = game["players"].get(sid)
    if not player or not is_bot_player(player):
        return
    del game["players"][sid]
    game["player_order"] = [item for item in game["player_order"] if item != sid]
    emit_state(room)


@socketio.on("update_bot_difficulty")
def on_update_bot_difficulty(data):
    room = data["room"].upper()
    game = rooms.get(room)
    if not game or request.sid != game["host_sid"] or game["status"] != "lobby":
        return
    set_bot_difficulty(game, data.get("sid"), data.get("difficulty", "medium"))
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
    if len(active_player_sids(game)) < 2:
        emit_error("You need at least 2 players.")
        return
    active_sids = active_player_sids(game)
    board_cards = board_size_from_settings(game["settings"]) * len(active_sids)
    deck_count = int(game["settings"].get("deck_count", 1))
    cards_available = deck_count * 52 + int(game["settings"].get("jokers", 2))
    if cards_available <= board_cards:
        emit_error("That grid needs more decks so at least one draw card remains.")
        return
    human_sids = [
        sid for sid in active_sids
        if not is_bot_player(game["players"][sid])
    ]
    solo_human_with_bots = len(human_sids) == 1
    if not all(
        game["players"][sid]["ready"]
        or is_bot_player(game["players"][sid])
        or solo_human_with_bots
        for sid in active_sids
    ):
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
    """Reveal a custom-grid opening card the viewer is allowed to inspect."""
    room = data["room"].upper()
    game = rooms.get(room)
    if not game or game["status"] != "playing":
        return
    if request.sid not in game["players"]:
        return
    player = game["players"][request.sid]
    if player["first_turn_started"] or player.get("eliminated"):
        emit_error("Opening peek is over.")
        return
    owner_sid = data.get("owner_sid", request.sid)
    try:
        index = int(data.get("index", -1))
    except (TypeError, ValueError):
        emit_error("Choose a valid card to peek at.")
        return
    if not can_opening_peek_slot(game, request.sid, owner_sid, index):
        emit_error("That card is not included in your opening peek settings.")
        return
    slot = slot_at(game, owner_sid, index)
    if not slot or not slot.get("card"):
        emit_error("That slot is empty.")
        return
    peeked = player.setdefault("opening_peeked", set())
    peeked.add(f"{owner_sid}:{index}")
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
    unlock_discard_card_for_burn(game, card)
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
            "inspected": [],
            "inspection_count": 0,
            "burned_selection": False,
            "burned_cards": [],
            "moved_cards": [],
            "can_switch": False,
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
    ability["inspected"] = []
    ability["inspection_count"] = 0
    ability["burned_selection"] = False
    ability["burned_cards"] = []
    ability["moved_cards"] = []
    ability["can_switch"] = False
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
        ability["inspected"] = []
        ability["inspection_count"] = 0
        ability["burned_selection"] = False
        ability["burned_cards"] = []
        ability["moved_cards"] = []
        ability["can_switch"] = False
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
        inspected = ability.setdefault("inspected", [])
        if candidate in inspected:
            emit_error("Choose two different card slots.")
            return
        if ability.get("inspection_count", len(inspected)) >= 2:
            emit_error("You already selected two cards.")
            return
        inspected.append(candidate)
        ability["inspection_count"] = len(inspected)
        selected.append(candidate)

        if ability_type == "switch_peek":
            refresh_switch_peek_cards(game, ability)
            ability["stage"] = (
                "deciding"
                if ability["inspection_count"] >= 2
                else "selecting"
            )
            emit_state(room)
            return

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

        ability["stage"] = "switching"
        selected_snapshot = deepcopy(selected)
        emit_state(room)
        socketio.start_background_task(
            resolve_unseen_switch,
            room,
            request.sid,
            selected_snapshot,
        )
        return

    begin_held_peek(game, request.sid, owner_sid, index)
    emit_state(room)


@socketio.on("ability_put_back")
def on_ability_put_back(data):
    room = data["room"].upper()
    game = rooms.get(room)
    if not game or game["status"] != "playing":
        return
    if not ensure_no_pending_burn(game):
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
    if not ensure_no_pending_burn(game):
        return
    peek = game.get("held_peek")
    if not peek or peek["sid"] != request.sid:
        emit_error("You are not holding a peeked card.")
        return
    if not game["discard_pile"]:
        emit_error("There is no discard to burn against.")
        return
    if is_discard_burn_locked(game):
        emit_error("That discard has already had a card burned on it.")
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
        # Card is already removed from the board while held; discard it now.
        discard_burned_card(game, target_card)
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
        if not ability.get("can_switch") or len(ability.get("selected", [])) != 2:
            emit_error("Those cards can only be put back.")
            return
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
        if ability.get("selected"):
            set_last_action(
                game,
                "ability_put_back",
                sid=request.sid,
                cards=deepcopy(ability["selected"]),
            )
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
    if ability.get("type") in {"switch_unseen", "switch_peek"} and ability.get("selected"):
        set_last_action(
            game,
            "ability_put_back",
            sid=request.sid,
            cards=deepcopy(ability["selected"]),
        )
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
    if ability.get("type") in {"switch_unseen", "switch_peek"} and ability.get("selected"):
        set_last_action(
            game,
            "ability_put_back",
            sid=request.sid,
            cards=deepcopy(ability["selected"]),
        )
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
        game["final_turns_remaining"] = [
            sid for sid in active_player_sids(game) if sid != request.sid
        ]
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

    owner_sid = data.get("owner_sid")
    index = int(data.get("index", -1))
    ability = game.get("pending_ability")
    inspection_burn = bool(
        ability
        and ability.get("sid") == request.sid
        and ability.get("type") == "switch_peek"
        and {"owner_sid": owner_sid, "index": index}
        in ability.get("selected", [])
    )
    outcome, target_card, err = resolve_burn_attempt(
        game,
        request.sid,
        owner_sid,
        index,
        data.get("discard_id"),
        inspection_burn=inspection_burn,
    )
    if outcome == "error":
        emit_error(err)
        emit_state(room)
        return
    if outcome == "pending":
        emit(
            "burn_attempt_registered",
            {
                "owner_sid": owner_sid,
                "index": index,
                "contest_window_ms": int(round(BURN_CONTEST_SECONDS * 1000)),
            },
            room=request.sid,
        )


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
    if target_slot and target_slot.get("card"):
        game["pending_burn"] = None
        refresh_final_countdown(game)
        emit_error("That burn target changed.")
        emit_state(room)
        return

    given_card = give_slot["card"]
    given_label = given_card["label"]
    burned_label = pending["target_card"]["label"]
    record_switch_peek_give(game, request.sid, give_index, given_card)
    game["players"][pending["target_sid"]]["board"][pending["target_index"]] = {
        "card": given_card,
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

    player = game["players"][request.sid]
    player["connected"] = False
    add_log(game, f"{player_name(game, request.sid)} disconnected.")
    if game["status"] == "lobby":
        socketio.start_background_task(
            cleanup_disconnected_lobby_player,
            room,
            request.sid,
            player.get("reconnect_token", ""),
        )

    emit_state(room)
