"""Server-side bot configuration, scheduling, memory-free strategy, and actions."""

from copy import deepcopy
import random
import time

from extensions import socketio
from game import (
    active_player_sids,
    add_burn_blocker,
    add_log,
    all_switchable_slots,
    burn_matches,
    can_opening_peek_slot,
    card_count,
    current_sid,
    discard_burned_card,
    discard_card,
    is_burn_blocked,
    is_discard_burn_locked,
    is_slot_burnt,
    is_bot_player,
    live_slots_for,
    make_player,
    make_slot,
    mark_slot_burnt,
    mark_turn_started,
    player_name,
    public_card,
    reset_discard_burn_state,
    slot_at,
    swap_slots,
    unlock_discard_card_for_burn,
)


BOT_NAMES = ["Mina", "Jax", "Rin", "Theo", "Zara"]
UNKNOWN_CARD_VALUE = (336 - 4) / 54
BOT_DIFFICULTIES = {"easy", "medium", "hard", "sweat", "custom"}
BOT_CONFIG = {
    "easy": {
        "reaction": (3.2, 5.2),
        "memory_error": 0.22,
        "burn_miss_rate": 0.08,
        "swap_gain": 6,
        "discard_gain": 5,
        "take_low_value": 1,
        "take_low_min_gain": 1,
        "ability_rate": 0.25,
        "peek_own_value": 0.5,
        "peek_other_value": 0.25,
        "switch_unseen_value": 1.0,
        "switch_peek_value": 1.5,
        "call_score": -1,
        "call_rate": 0.45,
        "call_card_count": 0,
        "call_card_score": -1,
        "final_call_score": -1,
        "final_call_rate": 0.35,
        "competitive_call_score": -1,
        "competitive_call_margin": 99,
        "burn_own_min": 9,
        "burn_opponent_gain": 99,
        "peek_burn_rate": 0.25,
        "random_swap": 0.28,
        "switch_random_rate": 1.0,
        "switch_execute_rate": 0.55,
        "switch_own_min": 99,
        "switch_target_lowest": 0,
        "mistake": 0.35,
    },
    "medium": {
        "reaction": (0.9, 1.6),
        "memory_error": 0.012,
        "burn_miss_rate": 0.003,
        "swap_gain": 0.35,
        "discard_gain": 0.25,
        "take_low_value": 0,
        "take_low_min_gain": 0,
        "ability_rate": 0.99,
        "peek_own_value": 2.0,
        "peek_other_value": 1.0,
        "switch_unseen_value": 3.0,
        "switch_peek_value": 5.0,
        "call_score": 6,
        "call_rate": 1.0,
        "call_card_count": 3,
        "call_card_score": 7,
        "final_call_score": 10,
        "final_call_rate": 1.0,
        "competitive_call_score": 10,
        "competitive_call_margin": 2,
        "burn_own_min": 1,
        "burn_opponent_gain": 0,
        "peek_burn_rate": 1.0,
        "random_swap": 0.005,
        "switch_random_rate": 0.0,
        "switch_execute_rate": 1.0,
        "switch_own_min": 1,
        "switch_target_lowest": 1.0,
        "mistake": 0.01,
    },
    "hard": {
        "reaction": (0.3, 0.7),
        "memory_error": 0.0,
        "burn_miss_rate": 0.0,
        "swap_gain": 0.05,
        "discard_gain": 0.05,
        "take_low_value": 0,
        "take_low_min_gain": 0,
        "ability_rate": 1.0,
        "peek_own_value": 3.0,
        "peek_other_value": 1.5,
        "switch_unseen_value": 5.0,
        "switch_peek_value": 8.0,
        "call_score": 8,
        "call_rate": 1.0,
        "call_card_count": 3,
        "call_card_score": 9,
        "final_call_score": 12,
        "final_call_rate": 1.0,
        "competitive_call_score": 12,
        "competitive_call_margin": 1,
        "burn_own_min": -2,
        "burn_opponent_gain": -2,
        "peek_burn_rate": 1.0,
        "random_swap": 0.0,
        "switch_random_rate": 0.0,
        "switch_execute_rate": 1.0,
        "switch_own_min": -2,
        "switch_target_lowest": 1,
        "mistake": 0.0,
    },
    "sweat": {
        "reaction": (0.01, 0.04),
        "instant_actions": True,
        "memory_error": 0.0,
        "burn_miss_rate": 0.0,
        "swap_gain": 0.001,
        "discard_gain": 0.001,
        "take_low_value": 0,
        "take_low_min_gain": 0.001,
        "ability_rate": 1.0,
        "peek_own_value": 3.5,
        "peek_other_value": 2.0,
        "switch_unseen_value": 6.0,
        "switch_peek_value": 10.0,
        "call_score": 10,
        "call_rate": 1.0,
        "call_card_count": 4,
        "call_card_score": 12,
        "final_call_score": 14,
        "final_call_rate": 1.0,
        "competitive_call_score": 14,
        "competitive_call_margin": 0.5,
        "burn_own_min": -2,
        "burn_opponent_gain": -2,
        "peek_burn_rate": 1.0,
        "random_swap": 0.0,
        "switch_random_rate": 0.0,
        "switch_execute_rate": 1.0,
        "switch_own_min": -2,
        "switch_target_lowest": 1.0,
        "mistake": 0.0,
    },
}


def runtime():
    # Imported lazily so multiplayer.py can import this module without a cycle.
    import multiplayer

    return multiplayer


def bot_sid(room, number):
    return f"BOT:{room}:{number}"


def normalize_bot_policy(raw_policy, difficulty):
    policy = deepcopy(BOT_CONFIG.get(difficulty, BOT_CONFIG["medium"]))
    if not isinstance(raw_policy, dict):
        return policy

    reaction = raw_policy.get("reaction")
    if isinstance(reaction, (list, tuple)) and len(reaction) == 2:
        try:
            low = max(0.3, min(8.0, float(reaction[0])))
            high = max(low, min(10.0, float(reaction[1])))
            policy["reaction"] = (low, high)
        except (TypeError, ValueError):
            pass

    bounds = {
        "mistake": (0.0, 0.8),
        "memory_error": (0.0, 0.6),
        "burn_miss_rate": (0.0, 0.25),
        "random_swap": (0.0, 1.0),
        "swap_gain": (-5.0, 20.0),
        "discard_gain": (-5.0, 20.0),
        "take_low_value": (-2.0, 13.0),
        "take_low_min_gain": (-5.0, 20.0),
        "ability_rate": (0.0, 1.0),
        "peek_own_value": (0.0, 20.0),
        "peek_other_value": (0.0, 20.0),
        "switch_unseen_value": (0.0, 20.0),
        "switch_peek_value": (0.0, 20.0),
        "peek_burn_rate": (0.0, 1.0),
        "call_score": (-5.0, 30.0),
        "call_rate": (0.0, 1.0),
        "call_card_count": (0.0, 12.0),
        "call_card_score": (-5.0, 30.0),
        "final_call_score": (-5.0, 30.0),
        "final_call_rate": (0.0, 1.0),
        "competitive_call_score": (-5.0, 30.0),
        "competitive_call_margin": (0.0, 99.0),
        "burn_own_min": (-2.0, 99.0),
        "burn_opponent_gain": (-5.0, 99.0),
        "switch_random_rate": (0.0, 1.0),
        "switch_execute_rate": (0.0, 1.0),
        "switch_own_min": (-2.0, 99.0),
        "switch_target_lowest": (0.0, 1.0),
    }
    for key, (lower, upper) in bounds.items():
        if key not in raw_policy:
            continue
        try:
            policy[key] = max(lower, min(upper, float(raw_policy[key])))
        except (TypeError, ValueError):
            continue
    return policy


def configure_bots(game, room, count, difficulty, bot_policy=None):
    difficulty = difficulty if difficulty in BOT_DIFFICULTIES else "medium"
    try:
        count = max(1, min(5, int(count)))
    except (TypeError, ValueError):
        count = 2
    game["bot_mode"] = True

    existing = [sid for sid, player in game["players"].items() if is_bot_player(player)]
    for sid in existing:
        del game["players"][sid]
        game["player_order"] = [item for item in game["player_order"] if item != sid]

    for number in range(1, count + 1):
        sid = bot_sid(room, number)
        policy = normalize_bot_policy(bot_policy, difficulty)
        game["players"][sid] = make_player(
            f"{BOT_NAMES[(number - 1) % len(BOT_NAMES)]} Bot",
            is_bot=True,
            difficulty=difficulty,
            bot_policy=policy,
        )
        game["player_order"].append(sid)


def next_bot_sid(game, room):
    number = 1
    while bot_sid(room, number) in game["players"]:
        number += 1
    return bot_sid(room, number), number


def add_bot(game, room, difficulty="medium"):
    difficulty = difficulty if difficulty in BOT_DIFFICULTIES else "medium"
    sid, number = next_bot_sid(game, room)
    name_index = sum(
        1 for player in game["players"].values() if is_bot_player(player)
    )
    game["players"][sid] = make_player(
        f"{BOT_NAMES[name_index % len(BOT_NAMES)]} Bot",
        is_bot=True,
        difficulty=difficulty,
        bot_policy=normalize_bot_policy(None, difficulty),
    )
    game["player_order"].append(sid)
    game["bot_mode"] = True
    return sid


def set_bot_difficulty(game, sid, difficulty):
    player = game["players"].get(sid)
    if not player or not is_bot_player(player):
        return False
    difficulty = difficulty if difficulty in BOT_DIFFICULTIES else "medium"
    player["difficulty"] = difficulty
    player["bot_policy"] = normalize_bot_policy(None, difficulty)
    return True


def bot_config(player):
    return player.get("bot_policy") or BOT_CONFIG.get(
        player.get("difficulty"),
        BOT_CONFIG["medium"],
    )


def bot_pressure(game, sid):
    """Return 0..1 pressure from bust risk, standings, and recent round cost."""
    player = game["players"].get(sid, {})
    if player.get("difficulty") not in {"medium", "custom"}:
        return 0.0
    target = max(1, int(game.get("settings", {}).get("target_score", 50)))
    score = max(0, float(player.get("score", 0)))
    opponents = [
        game["players"][other_sid].get("score", 0)
        for other_sid in active_player_sids(game)
        if other_sid != sid
    ]
    best_opponent = min(opponents) if opponents else score
    bust_pressure = max(0.0, min(1.0, (score / target - 0.4) / 0.5))
    trailing_pressure = max(
        0.0,
        min(1.0, (score - best_opponent) / max(8.0, target * 0.45)),
    )
    rounds = player.get("bot_telemetry", {}).get("rounds", [])[-3:]
    if rounds:
        average_round = sum(item.get("points", 0) for item in rounds) / len(rounds)
        history_pressure = max(0.0, min(1.0, average_round / max(8.0, target * 0.28)))
    else:
        history_pressure = 0.0
    return max(bust_pressure, trailing_pressure, history_pressure * 0.55)


def strategy_config(game, sid):
    """Return the current policy, with Medium tightening up when it is losing."""
    config = deepcopy(bot_config(game["players"][sid]))
    pressure = bot_pressure(game, sid)
    config["pressure"] = pressure
    if pressure <= 0:
        return config
    low, high = config["reaction"]
    speed = 1 - 0.28 * pressure
    config["reaction"] = (max(0.8, low * speed), max(1.3, high * speed))
    config["mistake"] *= 1 - 0.8 * pressure
    config["memory_error"] *= 1 - 0.7 * pressure
    config["burn_miss_rate"] *= 1 - 0.75 * pressure
    config["random_swap"] *= 1 - 0.8 * pressure
    config["swap_gain"] = max(0.35, config["swap_gain"] - 0.8 * pressure)
    config["discard_gain"] = max(0.35, config["discard_gain"] - 0.8 * pressure)
    config["ability_rate"] = min(1.0, config["ability_rate"] + 0.08 * pressure)
    config["call_score"] += 3.0 * pressure
    config["call_card_score"] += 2.0 * pressure
    config["final_call_score"] += 1.5 * pressure
    config["call_rate"] = min(1.0, config["call_rate"] + 0.02 * pressure)
    config["burn_own_min"] = max(-2, config["burn_own_min"] - 2.0 * pressure)
    config["burn_opponent_gain"] = max(-2, config["burn_opponent_gain"] - 2.0 * pressure)
    config["peek_burn_rate"] = min(1.0, config["peek_burn_rate"] + 0.06 * pressure)
    config["switch_execute_rate"] = min(
        1.0,
        config["switch_execute_rate"] + 0.02 * pressure,
    )
    config["switch_target_lowest"] = min(
        1.0,
        config["switch_target_lowest"] + 0.2 * pressure,
    )
    return config


def record_bot_event(game, sid, event, **details):
    """Keep bounded, room-local strategy telemetry without exposing hidden cards."""
    player = game["players"].get(sid)
    if not player or not is_bot_player(player):
        return
    telemetry = player.setdefault(
        "bot_telemetry",
        {"decisions": 0, "rounds": [], "events": []},
    )
    entry = {
        "round": game.get("round_number", 0),
        "event": event,
        "score": player.get("score", 0),
        "pressure": round(bot_pressure(game, sid), 3),
        **details,
    }
    telemetry["decisions"] = telemetry.get("decisions", 0) + 1
    telemetry.setdefault("events", []).append(entry)
    telemetry["events"] = telemetry["events"][-80:]
    game.setdefault("bot_match_log", []).append({"sid": sid, **entry})
    game["bot_match_log"] = game["bot_match_log"][-300:]


def record_bot_round_outcomes(game, hand_scores, round_scores):
    for sid, points in round_scores.items():
        player = game["players"].get(sid)
        if not player or not is_bot_player(player):
            continue
        telemetry = player.setdefault(
            "bot_telemetry",
            {"decisions": 0, "rounds": [], "events": []},
        )
        result = {
            "round": game.get("round_number", 0),
            "hand": hand_scores.get(sid, 0),
            "points": points,
            "total": player.get("score", 0),
        }
        telemetry.setdefault("rounds", []).append(result)
        telemetry["rounds"] = telemetry["rounds"][-20:]
        record_bot_event(game, sid, "round_result", **result)


def memory_key(owner_sid, index):
    return f"{owner_sid}:{index}"


def remember_bot_card(game, observer_sid, owner_sid, index, card=None):
    """Remember a card only after the bot has legitimately seen it."""
    player = game["players"].get(observer_sid)
    if not player or not is_bot_player(player):
        return
    if card is None:
        slot = slot_at(game, owner_sid, index)
        card = slot.get("card") if slot else None
    if not card:
        return
    player.setdefault("bot_known_cards", {})[memory_key(owner_sid, index)] = {
        "id": card["id"],
        "value": card["value"],
        "burn_key": card["burn_key"],
    }


def forget_bot_card_for_all(game, owner_sid, index):
    key = memory_key(owner_sid, index)
    for player in game["players"].values():
        if is_bot_player(player):
            player.setdefault("bot_known_cards", {}).pop(key, None)


def swap_bot_knowledge(game, first_owner, first_index, second_owner, second_index):
    """Bots can track known face-down cards through a visible slot swap."""
    first_key = memory_key(first_owner, first_index)
    second_key = memory_key(second_owner, second_index)
    for player in game["players"].values():
        if not is_bot_player(player):
            continue
        memory = player.setdefault("bot_known_cards", {})
        first = memory.pop(first_key, None)
        second = memory.pop(second_key, None)
        if first is not None:
            memory[second_key] = first
        if second is not None:
            memory[first_key] = second


def initialize_bot_round_knowledge(game):
    """Give bots only the opening information allowed by the custom grid."""
    for sid, player in game["players"].items():
        if not is_bot_player(player) or player.get("eliminated"):
            continue
        player["bot_known_cards"] = {}
        for owner_sid in active_player_sids(game):
            for index, _ in live_slots_for(game, owner_sid):
                if can_opening_peek_slot(game, sid, owner_sid, index):
                    remember_bot_card(game, sid, owner_sid, index)


def unknown_card_value(game):
    jokers = max(0, int(game["settings"].get("jokers", 2)))
    joker_value = int(game["settings"].get("joker_value", -2))
    deck_count = max(1, int(game["settings"].get("deck_count", 1)))
    # Standard non-joker Shmamale deck totals 336 points.
    return (336 * deck_count + jokers * joker_value) / (52 * deck_count + jokers)


def known_card_info(game, observer_sid, owner_sid, index):
    slot = slot_at(game, owner_sid, index)
    if not slot or not slot.get("card"):
        return None
    card = slot["card"]
    if slot.get("revealed"):
        return {
            "id": card["id"],
            "value": card["value"],
            "burn_key": card["burn_key"],
        }
    player = game["players"].get(observer_sid, {})
    remembered = player.get("bot_known_cards", {}).get(memory_key(owner_sid, index))
    if remembered and remembered.get("id") == card["id"]:
        return remembered
    return None


def perceived_slot_value(game, observer_sid, owner_sid, index, config=None):
    known = known_card_info(game, observer_sid, owner_sid, index)
    if known and config and random.random() < config.get("memory_error", 0):
        known = None
    return known["value"] if known else unknown_card_value(game)


def estimated_board_score(game, sid, config=None):
    config = config or strategy_config(game, sid)
    return estimated_player_board_score(game, sid, sid, config)


def estimated_player_board_score(game, observer_sid, owner_sid, config=None):
    config = config or strategy_config(game, observer_sid)
    return sum(
        perceived_slot_value(game, observer_sid, owner_sid, index, config)
        for index, _ in live_slots_for(game, owner_sid)
    )


def best_swap_slot(game, sid, new_card, config):
    own_slots = live_slots_for(game, sid)
    if not own_slots:
        return None, 0
    perceived = [
        (index, perceived_slot_value(game, sid, sid, index, config))
        for index, _ in own_slots
    ]
    if random.random() < config["random_swap"]:
        index, old_value = random.choice(perceived)
        return index, old_value - new_card["value"]
    index, old_value = max(perceived, key=lambda item: item[1])
    return index, old_value - new_card["value"]


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

    if game.get("active_burn_contest_id"):
        return None

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
    mp = runtime()
    game = mp.rooms.get(room)
    if not game:
        return
    maybe_schedule_bot_burn(room)
    key = bot_action_key(game)
    if not key or game.get("bot_scheduled_key") == key:
        return
    game["bot_scheduled_key"] = key
    socketio.start_background_task(run_bot_work, room, key)


def run_bot_work(room, key):
    mp = runtime()
    game = mp.rooms.get(room)
    sid = key[1]
    if not game or sid not in game["players"]:
        return
    config = strategy_config(game, sid)
    low, high = config["reaction"]
    if not config.get("instant_actions"):
        if key[0] == "held_peek":
            difficulty = game["players"][sid].get("difficulty", "medium")
            peek_floor = {
                "easy": (3.0, 4.5),
                "medium": (1.0, 1.7),
                "hard": (0.45, 0.85),
                "custom": (1.0, 1.7),
            }.get(difficulty, (1.0, 1.7))
            low, high = max(low, peek_floor[0]), max(high, peek_floor[1])
        elif key[0] == "ability":
            low, high = max(low * 0.85, 0.3), max(high * 0.95, 0.6)
    socketio.sleep(random.uniform(low, high))

    game = mp.rooms.get(room)
    if not game or game.get("bot_scheduled_key") != key:
        return
    game["bot_scheduled_key"] = None
    if game.get("active_burn_contest_id"):
        return
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
    mp.emit_state(room)


def maybe_schedule_bot_burn(room):
    mp = runtime()
    game = mp.rooms.get(room)
    if not game or game["status"] != "playing" or game.get("pending_burn"):
        return
    if game["phase"] not in {"choose", "ability", "final_countdown"}:
        return
    if game.get("pending_draw") or not game["discard_pile"]:
        return
    top_card = game["discard_pile"][-1]
    if is_discard_burn_locked(game, top_card):
        return
    checked = game.setdefault("bot_burn_checked", set())
    pending = game.setdefault("bot_burn_pending", set())
    knowledge_epoch = game.get("burn_knowledge_epoch", 0)
    contest = game.get("burn_contests", {}).get(top_card["id"], {})
    attempted_sids = {attempt["sid"] for attempt in contest.get("attempts", [])}
    for sid in game["player_order"]:
        if (
            sid not in game["players"]
            or not is_bot_player(game["players"][sid])
            or game["players"][sid].get("eliminated")
        ):
            continue
        pending_key = (sid, top_card["id"])
        if pending_key in pending or sid in attempted_sids:
            continue
        check_key = (sid, top_card["id"], knowledge_epoch)
        if check_key in checked:
            continue
        checked.add(check_key)
        candidate = choose_bot_burn_candidate(game, top_card, sid)
        if not candidate:
            continue
        pending.add(pending_key)
        low, high = strategy_config(game, sid)["reaction"]
        socketio.start_background_task(
            run_bot_burn,
            room,
            top_card["id"],
            candidate,
            random.uniform(low, high),
        )


def run_bot_burn(room, discard_card_id, candidate, delay):
    mp = runtime()
    socketio.sleep(delay)
    game = mp.rooms.get(room)
    if not game or game["status"] != "playing":
        return
    game.setdefault("bot_burn_pending", set()).discard(
        (candidate["sid"], discard_card_id)
    )
    perform_bot_burn(game, candidate, discard_card_id, time.time())


def choose_bot_burn_candidate(game, top_card, bot_sid=None):
    if bot_sid is not None:
        bot_sids = [bot_sid]
    else:
        bot_sids = [
            sid
            for sid in active_player_sids(game)
            if is_bot_player(game["players"][sid])
        ]
        random.shuffle(bot_sids)
    for sid in bot_sids:
        if game.get("pending_draw") and game["pending_draw"].get("sid") == sid:
            continue
        player = game["players"][sid]
        if player.get("called"):
            continue
        config = strategy_config(game, sid)
        if random.random() < config["mistake"]:
            continue
        eligible = [
            (index, slot, known_card_info(game, sid, sid, index))
            for index, slot in live_slots_for(game, sid)
            if not is_slot_burnt(game, sid, index)
            and not is_burn_blocked(game, sid, index, slot["card"]["id"])
        ]
        unknown = [(index, slot) for index, slot, known in eligible if known is None]
        if unknown and random.random() < config.get("burn_miss_rate", 0):
            index, _ = random.choice(unknown)
            return {"sid": sid, "owner_sid": sid, "index": index}
        matches = [
            (index, known)
            for index, _, known in eligible
            if known and known["burn_key"] == top_card["burn_key"]
        ]
        if matches:
            index, known = max(matches, key=lambda item: item[1]["value"])
            if known["value"] >= config["burn_own_min"]:
                return {"sid": sid, "owner_sid": sid, "index": index}

        own_slots = live_slots_for(game, sid)
        give_slot = max(
            own_slots,
            key=lambda item: perceived_slot_value(game, sid, sid, item[0], config),
        ) if own_slots else None
        if not give_slot:
            continue
        opponent_matches = []
        for owner_sid in active_player_sids(game):
            if owner_sid == sid or owner_sid not in game["players"]:
                continue
            for index, slot in live_slots_for(game, owner_sid):
                if not slot.get("revealed") or is_slot_burnt(game, owner_sid, index):
                    continue
                if burn_matches(top_card, slot["card"]):
                    opponent_matches.append((owner_sid, index, slot))
        if not opponent_matches:
            continue
        owner_sid, index, target = min(
            opponent_matches,
            key=lambda item: item[2]["card"]["value"],
        )
        give_value = perceived_slot_value(game, sid, sid, give_slot[0], config)
        if give_value - target["card"]["value"] >= config["burn_opponent_gain"]:
            return {"sid": sid, "owner_sid": owner_sid, "index": index}
    return None


def perform_bot_burn(game, candidate, discard_card_id=None, attempted_at=None):
    mp = runtime()
    sid = candidate["sid"]
    owner_sid = candidate["owner_sid"]
    index = candidate["index"]
    outcome, _, _ = mp.resolve_burn_attempt(
        game,
        sid,
        owner_sid,
        index,
        discard_card_id,
        attempted_at,
    )
    record_bot_event(game, sid, "burn", outcome=outcome, own=owner_sid == sid)
    return outcome


def perform_bot_burn_give(game, sid):
    mp = runtime()
    pending = game.get("pending_burn")
    if not pending or pending["sid"] != sid:
        return
    own_slots = live_slots_for(game, sid)
    if not own_slots:
        game["pending_burn"] = None
        mp.refresh_final_countdown(game)
        return
    config = strategy_config(game, sid)
    give_index, give_slot = max(
        own_slots,
        key=lambda item: perceived_slot_value(game, sid, sid, item[0], config),
    )
    given_card = give_slot["card"]
    burned_label = pending["target_card"]["label"]
    mp.record_switch_peek_give(game, sid, give_index, given_card)
    game["players"][pending["target_sid"]]["board"][pending["target_index"]] = {
        "card": given_card,
        "revealed": False,
    }
    game["players"][sid]["board"][give_index] = None
    forget_bot_card_for_all(game, pending["target_sid"], pending["target_index"])
    forget_bot_card_for_all(game, sid, give_index)
    remember_bot_card(
        game,
        sid,
        pending["target_sid"],
        pending["target_index"],
        given_card,
    )
    game["pending_burn"] = None
    mp.set_last_action(
        game,
        "burn_give",
        sid=sid,
        target_sid=pending["target_sid"],
        target_index=pending["target_index"],
        give_index=give_index,
    )
    add_log(
        game,
        f"{player_name(game, sid)} burned {burned_label} and gave "
        f"{given_card['label']} to {player_name(game, pending['target_sid'])}.",
    )
    record_bot_event(game, sid, "burn_give")
    if game.pop("_advance_after_burn_give", False):
        mp.advance_turn(game)


def bot_should_call(game, sid):
    player = game["players"][sid]
    config = strategy_config(game, sid)
    score = estimated_board_score(game, sid, config)
    cards = card_count(player["board"])
    base_cards = max(1, len(game["settings"].get("grid_peek_modes", [])) or 4)
    size_scale = base_cards / 4
    call_score = config["call_score"] * size_scale
    call_card_count = config["call_card_count"] * size_scale
    call_card_score = config["call_card_score"] * size_scale
    final_call_score = config["final_call_score"] * size_scale
    competitive_call_score = config.get("competitive_call_score", -1) * size_scale
    competitive_call_margin = config.get("competitive_call_margin", 99) * size_scale
    target = int(game["settings"].get("target_score", 50))
    safe_call_score = target - player.get("score", 0) - 1
    call_score = min(call_score, safe_call_score)
    call_card_score = min(call_card_score, safe_call_score)
    final_call_score = min(final_call_score, safe_call_score)
    competitive_call_score = min(competitive_call_score, safe_call_score)
    if player["called"]:
        return False
    if game["first_caller_sid"]:
        return score <= final_call_score and random.random() < config["final_call_rate"]
    should_call = score <= call_score or (
        call_card_count > 0
        and cards <= call_card_count
        and score <= call_card_score
    )
    opponent_scores = [
        estimated_player_board_score(game, sid, owner_sid, config)
        for owner_sid in active_player_sids(game)
        if owner_sid != sid
    ]
    likely_leading = bool(opponent_scores) and (
        score <= competitive_call_score
        and score + competitive_call_margin <= min(opponent_scores)
    )
    should_call = should_call or likely_leading
    return should_call and random.random() < config["call_rate"]


def bot_should_play_ability(game, sid, card, swap_gain, config=None):
    ability = card.get("ability")
    if not ability:
        return False
    config = config or strategy_config(game, sid)
    value_key = {
        "peek_own": "peek_own_value",
        "peek_other": "peek_other_value",
        "switch_unseen": "switch_unseen_value",
        "switch_peek": "switch_peek_value",
    }.get(ability)
    if not value_key:
        return False
    board_cards = max(1, len(game["settings"].get("grid_peek_modes", [])) or 4)
    board_scale = (board_cards / 4) ** 0.5
    ability_value = config.get(value_key, 0) * board_scale
    return swap_gain <= ability_value and random.random() < config["ability_rate"]


def perform_bot_choose(game, sid):
    mp = runtime()
    if current_sid(game) != sid or game["phase"] != "choose":
        return
    mark_turn_started(game)
    if bot_should_call(game, sid):
        perform_bot_call(game, sid)
        return
    config = strategy_config(game, sid)
    if game["discard_pile"]:
        top_card = game["discard_pile"][-1]
        _, gain = best_swap_slot(game, sid, top_card, config)
        should_take = gain >= config["discard_gain"]
        if top_card["value"] <= config["take_low_value"] and gain >= config["take_low_min_gain"]:
            should_take = True
        if random.random() < config["mistake"]:
            should_take = not should_take and random.random() < 0.4
        if should_take:
            card = game["discard_pile"].pop()
            unlock_discard_card_for_burn(game, card)
            reset_discard_burn_state(game)
            game["pending_draw"] = {"sid": sid, "card": card, "source": "discard"}
            game["phase"] = "drawn"
            mp.set_last_action(game, "take", sid=sid, card=public_card(card))
            add_log(game, f"{player_name(game, sid)} took the discard.")
            record_bot_event(game, sid, "take_discard", estimated_gain=round(gain, 2))
            return
    if game["draw_pile"]:
        card = game["draw_pile"].pop()
        game["pending_draw"] = {"sid": sid, "card": card, "source": "draw"}
        game["phase"] = "drawn"
        mp.set_last_action(game, "draw", sid=sid)
        add_log(game, f"{player_name(game, sid)} drew from the deck.")
        record_bot_event(game, sid, "draw")
        return
    if game["discard_pile"]:
        card = game["discard_pile"].pop()
        unlock_discard_card_for_burn(game, card)
        reset_discard_burn_state(game)
        game["pending_draw"] = {"sid": sid, "card": card, "source": "discard"}
        game["phase"] = "drawn"
        mp.set_last_action(game, "take", sid=sid, card=public_card(card))
        add_log(game, f"{player_name(game, sid)} took the last discard.")
        record_bot_event(game, sid, "take_last_discard")
        return
    perform_bot_call(game, sid)


def perform_bot_drawn(game, sid):
    pending = game.get("pending_draw")
    if current_sid(game) != sid or game["phase"] != "drawn" or not pending or pending["sid"] != sid:
        return
    config = strategy_config(game, sid)
    card = pending["card"]
    index, gain = best_swap_slot(game, sid, card, config)
    if pending["source"] == "discard":
        if index is not None:
            bot_swap_drawn(game, sid, index)
        return
    should_swap = index is not None and (
        gain >= config["swap_gain"] or (card["value"] <= 0 and gain >= 0)
    )
    if bot_should_play_ability(game, sid, card, gain, config):
        should_swap = False
    if random.random() < config["mistake"]:
        should_swap = not should_swap
    if should_swap and index is not None:
        bot_swap_drawn(game, sid, index)
    else:
        bot_play_drawn(game, sid)


def bot_swap_drawn(game, sid, index):
    mp = runtime()
    pending = game["pending_draw"]
    slot = slot_at(game, sid, index)
    if not slot or not slot.get("card"):
        bot_play_drawn(game, sid)
        return
    old_card = slot["card"]
    source = pending["source"]
    new_card = pending["card"]
    game["players"][sid]["board"][index] = make_slot(new_card)
    forget_bot_card_for_all(game, sid, index)
    remember_bot_card(game, sid, sid, index, new_card)
    discard_card(game, old_card)
    game["pending_draw"] = None
    if source == "discard":
        add_burn_blocker(game, sid, index, new_card["id"])
    mp.set_last_action(
        game,
        "swap",
        sid=sid,
        index=index,
        source=source,
        outgoing=public_card(old_card),
    )
    add_log(game, f"{player_name(game, sid)} switched a card and discarded {old_card['label']}.")
    record_bot_event(game, sid, "swap", source=source)
    mp.advance_turn(game)


def bot_play_drawn(game, sid):
    mp = runtime()
    pending = game["pending_draw"]
    card = pending["card"]
    discard_card(game, card)
    game["pending_draw"] = None
    mp.set_last_action(game, "play", sid=sid, card=public_card(card))
    add_log(game, f"{player_name(game, sid)} played {card['label']} to discard.")
    record_bot_event(game, sid, "play", ability=card.get("ability") or "")
    player = game["players"][sid]
    if card["ability"] and random.random() < strategy_config(game, sid)["ability_rate"]:
        game["phase"] = "ability"
        game["pending_ability"] = {
            "sid": sid,
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
        if card["ability"]:
            add_log(game, f"{player_name(game, sid)} skipped the special.")
        mp.advance_turn(game)


def perform_bot_put_back(game, sid):
    mp = runtime()
    peek = game.get("held_peek")
    if not peek or peek["sid"] != sid:
        return
    remember_bot_card(game, sid, peek["owner_sid"], peek["index"], peek["card"])
    if (
        peek.get("burnable")
        and game["discard_pile"]
        and not is_discard_burn_locked(game)
        and burn_matches(game["discard_pile"][-1], peek["card"])
        and random.random() < strategy_config(game, sid)["peek_burn_rate"]
    ):
        owner_sid = peek["owner_sid"]
        index = peek["index"]
        target_card = peek["card"]
        game["held_peek"] = None
        game["pending_ability"] = None
        if owner_sid == sid:
            discard_burned_card(game, target_card)
            mark_slot_burnt(game, owner_sid, index)
            mp.set_last_action(
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
            mp.advance_turn(game)
            return
        game["players"][owner_sid]["board"][index] = {"card": target_card, "revealed": True}
        ok, _ = mp.apply_successful_opponent_burn(game, sid, owner_sid, index, target_card)
        if ok:
            game["_advance_after_burn_give"] = True
            return
    mp.put_back_held_peek(game)


def perform_bot_ability(game, sid):
    mp = runtime()
    ability = game.get("pending_ability")
    if current_sid(game) != sid or game["phase"] != "ability" or not ability or ability["sid"] != sid:
        return
    if game.get("pending_burn"):
        return
    if ability["stage"] == "waiting":
        ability.update(
            {
                "stage": "selecting",
                "selected": [],
                "inspected": [],
                "inspection_count": 0,
                "burned_selection": False,
                "burned_cards": [],
                "moved_cards": [],
                "can_switch": False,
            }
        )
    ability_type = ability["type"]
    if ability_type == "peek_own":
        own_slots = live_slots_for(game, sid)
        if own_slots:
            unknown_slots = [
                item
                for item in own_slots
                if known_card_info(game, sid, sid, item[0]) is None
            ]
            if unknown_slots:
                index, _ = random.choice(unknown_slots)
            else:
                index, _ = max(
                    own_slots,
                    key=lambda item: perceived_slot_value(game, sid, sid, item[0]),
                )
            mp.begin_held_peek(game, sid, sid, index)
        else:
            game["pending_ability"] = None
            mp.advance_turn(game)
        return
    if ability_type == "peek_other":
        targets = [
            (owner_sid, index, slot)
            for owner_sid in active_player_sids(game)
            if owner_sid != sid and owner_sid in game["players"]
            for index, slot in live_slots_for(game, owner_sid)
        ]
        if targets:
            unknown_targets = [
                item
                for item in targets
                if known_card_info(game, sid, item[0], item[1]) is None
            ]
            owner_sid, index, _ = random.choice(unknown_targets or targets)
            mp.begin_held_peek(game, sid, owner_sid, index)
        else:
            game["pending_ability"] = None
            mp.advance_turn(game)
        return
    if ability_type in {"switch_unseen", "switch_peek"}:
        pair = choose_bot_switch_pair(game, sid, ability_type == "switch_peek")
        if pair:
            first, second, should_switch = pair
            if should_switch:
                swap_slots(game, first[0], first[1], second[0], second[1])
                swap_bot_knowledge(game, first[0], first[1], second[0], second[1])
                mp.set_last_action(
                    game,
                    "switch",
                    sid=sid,
                    a={"owner_sid": first[0], "index": first[1]},
                    b={"owner_sid": second[0], "index": second[1]},
                )
                label = "looked and switched two cards" if ability_type == "switch_peek" else "switched two unseen cards"
                add_log(game, f"{player_name(game, sid)} {label}.")
            else:
                add_log(game, f"{player_name(game, sid)} looked and kept the cards in place.")
        else:
            add_log(game, f"{player_name(game, sid)} skipped the special.")
        game["pending_ability"] = None
        mp.advance_turn(game)


def choose_bot_switch_pair(game, sid, can_peek):
    config = strategy_config(game, sid)
    slots = all_switchable_slots(game, sid)
    if len(slots) < 2:
        return None
    if random.random() < config["switch_random_rate"]:
        first, second = random.sample(slots, 2)
        first_choice = (first[0], first[1])
        second_choice = (second[0], second[1])
        if can_peek:
            remember_bot_card(game, sid, first[0], first[1], first[2]["card"])
            remember_bot_card(game, sid, second[0], second[1], second[2]["card"])
        return first_choice, second_choice, random.random() < config["switch_execute_rate"]
    own_slots = [(sid, index, slot) for index, slot in live_slots_for(game, sid)]
    opponent_slots = [item for item in slots if item[0] != sid]
    if not own_slots or not opponent_slots:
        first, second = random.sample(slots, 2)
        return (
            (first[0], first[1]),
            (second[0], second[1]),
            can_peek and random.random() < config["switch_execute_rate"],
        )
    own_high = max(
        own_slots,
        key=lambda item: perceived_slot_value(game, sid, sid, item[1], config),
    )
    if random.random() < config["switch_target_lowest"]:
        opponent = min(
            opponent_slots,
            key=lambda item: perceived_slot_value(game, sid, item[0], item[1], config),
        )
    else:
        opponent = random.choice(opponent_slots)
    if can_peek:
        remember_bot_card(game, sid, own_high[0], own_high[1], own_high[2]["card"])
        remember_bot_card(game, sid, opponent[0], opponent[1], opponent[2]["card"])
    own_value = perceived_slot_value(game, sid, own_high[0], own_high[1])
    opponent_value = perceived_slot_value(game, sid, opponent[0], opponent[1])
    should_switch = own_value > opponent_value and own_value >= config["switch_own_min"]
    should_switch = should_switch and random.random() < config["switch_execute_rate"]
    return (
        (own_high[0], own_high[1]),
        (opponent[0], opponent[1]),
        should_switch,
    )


def perform_bot_call(game, sid):
    mp = runtime()
    player = game["players"][sid]
    player["called"] = True
    player["protected"] = True
    record_bot_event(game, sid, "call")
    if not game["first_caller_sid"]:
        game["first_caller_sid"] = sid
        game["final_turns_remaining"] = [
            player_sid for player_sid in active_player_sids(game) if player_sid != sid
        ]
        add_log(game, f"{player_name(game, sid)} called. Everyone else gets one final turn.")
    else:
        add_log(game, f"{player_name(game, sid)} called to protect their cards.")
    mp.advance_turn(game)
