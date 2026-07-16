"""Server-side bot configuration, scheduling, memory-free strategy, and actions."""

from copy import deepcopy
import random

from extensions import socketio
from game import (
    add_burn_blocker,
    add_log,
    all_switchable_slots,
    burn_matches,
    can_attempt_burn,
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
    score_board,
    slot_at,
    swap_slots,
    unlock_discard_card_for_burn,
)


BOT_NAMES = ["Mina", "Jax", "Rin", "Theo", "Zara"]
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
        "call_score": -1,
        "call_rate": 0.45,
        "call_card_count": 0,
        "call_card_score": -1,
        "final_call_score": -1,
        "final_call_rate": 0.35,
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
        "reaction": (2.5, 4.5),
        "memory_error": 0.06,
        "burn_miss_rate": 0.02,
        "swap_gain": 2,
        "discard_gain": 2,
        "take_low_value": 0,
        "take_low_min_gain": 0,
        "ability_rate": 0.82,
        "call_score": 1,
        "call_rate": 0.9,
        "call_card_count": 2,
        "call_card_score": 4,
        "final_call_score": 7,
        "final_call_rate": 0.95,
        "burn_own_min": 5,
        "burn_opponent_gain": 4,
        "peek_burn_rate": 0.85,
        "random_swap": 0.05,
        "switch_random_rate": 0.0,
        "switch_execute_rate": 0.94,
        "switch_own_min": 6,
        "switch_target_lowest": 0.6,
        "mistake": 0.08,
    },
    "hard": {
        "reaction": (1.6, 3.0),
        "memory_error": 0.025,
        "burn_miss_rate": 0.008,
        "swap_gain": 1,
        "discard_gain": 1,
        "take_low_value": 0,
        "take_low_min_gain": 0,
        "ability_rate": 0.95,
        "call_score": 0,
        "call_rate": 1.0,
        "call_card_count": 3,
        "call_card_score": 3,
        "final_call_score": 7,
        "final_call_rate": 1.0,
        "burn_own_min": 1,
        "burn_opponent_gain": 0,
        "peek_burn_rate": 0.98,
        "random_swap": 0.01,
        "switch_random_rate": 0.0,
        "switch_execute_rate": 1.0,
        "switch_own_min": -2,
        "switch_target_lowest": 1,
        "mistake": 0.03,
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
        "peek_burn_rate": (0.0, 1.0),
        "call_score": (-5.0, 30.0),
        "call_rate": (0.0, 1.0),
        "call_card_count": (0.0, 12.0),
        "call_card_score": (-5.0, 30.0),
        "final_call_score": (-5.0, 30.0),
        "final_call_rate": (0.0, 1.0),
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
    difficulty = difficulty if difficulty in BOT_CONFIG else "medium"
    try:
        count = max(1, min(5, int(count)))
    except (TypeError, ValueError):
        count = 2
    game["bot_mode"] = True
    game["settings"]["bot_count"] = count
    game["settings"]["bot_difficulty"] = difficulty

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


def bot_config(player):
    return player.get("bot_policy") or BOT_CONFIG.get(
        player.get("difficulty"),
        BOT_CONFIG["medium"],
    )


def best_swap_slot(game, sid, new_card, config):
    own_slots = live_slots_for(game, sid)
    if not own_slots:
        return None, 0
    if random.random() < config.get("memory_error", 0):
        index, slot = random.choice(own_slots)
        return index, slot["card"]["value"] - new_card["value"]
    if random.random() < config["random_swap"]:
        index, slot = random.choice(own_slots)
        return index, slot["card"]["value"] - new_card["value"]
    index, slot = max(own_slots, key=lambda item: item[1]["card"]["value"])
    return index, slot["card"]["value"] - new_card["value"]


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
    low, high = bot_config(game["players"][sid])["reaction"]
    if key[0] == "held_peek":
        low, high = max(low, 3.0), max(high, 4.5)
    elif key[0] == "ability":
        low, high = max(low * 0.85, 1.4), max(high * 0.95, 2.5)
    socketio.sleep(random.uniform(low, high))

    game = mp.rooms.get(room)
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
    if game.get("bot_burn_checked_card_id") == top_card["id"]:
        return
    candidate = choose_bot_burn_candidate(game, top_card)
    game["bot_burn_checked_card_id"] = top_card["id"]
    if not candidate:
        return
    low, high = bot_config(game["players"][candidate["sid"]])["reaction"]
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
    if (
        not game
        or game["status"] != "playing"
        or game.get("pending_burn")
        or not game["discard_pile"]
        or game["discard_pile"][-1]["id"] != discard_card_id
    ):
        return
    perform_bot_burn(game, candidate)
    mp.emit_state(room)


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
        if player.get("called"):
            continue
        config = bot_config(player)
        if random.random() < config["mistake"]:
            continue
        eligible = [
            (index, slot)
            for index, slot in live_slots_for(game, sid)
            if not is_slot_burnt(game, sid, index)
            and not is_burn_blocked(game, sid, index, slot["card"]["id"])
        ]
        wrong = [(index, slot) for index, slot in eligible if not burn_matches(top_card, slot["card"])]
        if wrong and random.random() < config.get("burn_miss_rate", 0):
            index, _ = random.choice(wrong)
            return {"sid": sid, "owner_sid": sid, "index": index}
        matches = [(index, slot) for index, slot in eligible if burn_matches(top_card, slot["card"])]
        if matches:
            index, slot = max(matches, key=lambda item: item[1]["card"]["value"])
            if slot["card"]["value"] >= config["burn_own_min"]:
                return {"sid": sid, "owner_sid": sid, "index": index}

        own_slots = live_slots_for(game, sid)
        give_slot = max(own_slots, key=lambda item: item[1]["card"]["value"]) if own_slots else None
        if not give_slot:
            continue
        opponent_matches = []
        for owner_sid in game["player_order"]:
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
        if give_slot[1]["card"]["value"] - target["card"]["value"] >= config["burn_opponent_gain"]:
            return {"sid": sid, "owner_sid": owner_sid, "index": index}
    return None


def perform_bot_burn(game, candidate):
    mp = runtime()
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
        mp.apply_failed_burn(game, sid, owner_sid, index, target_card, "rank")
    elif owner_sid == sid:
        mp.apply_successful_own_burn(game, sid, owner_sid, index, target_card)
    else:
        mp.apply_successful_opponent_burn(game, sid, owner_sid, index, target_card)


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
    give_index, give_slot = max(own_slots, key=lambda item: item[1]["card"]["value"])
    given_card = give_slot["card"]
    burned_label = pending["target_card"]["label"]
    mp.record_switch_peek_give(game, sid, give_index, given_card)
    game["players"][pending["target_sid"]]["board"][pending["target_index"]] = {
        "card": given_card,
        "revealed": False,
    }
    game["players"][sid]["board"][give_index] = None
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
    if game.pop("_advance_after_burn_give", False):
        mp.advance_turn(game)


def bot_should_call(game, sid):
    player = game["players"][sid]
    config = bot_config(player)
    score = score_board(player["board"])
    cards = card_count(player["board"])
    if player["called"]:
        return False
    if game["first_caller_sid"]:
        return score <= config["final_call_score"] and random.random() < config["final_call_rate"]
    should_call = score <= config["call_score"] or (
        config["call_card_count"] > 0
        and cards <= config["call_card_count"]
        and score <= config["call_card_score"]
    )
    return should_call and random.random() < config["call_rate"]


def perform_bot_choose(game, sid):
    mp = runtime()
    if current_sid(game) != sid or game["phase"] != "choose":
        return
    mark_turn_started(game)
    if bot_should_call(game, sid):
        perform_bot_call(game, sid)
        return
    config = bot_config(game["players"][sid])
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
            return
    if game["draw_pile"]:
        card = game["draw_pile"].pop()
        game["pending_draw"] = {"sid": sid, "card": card, "source": "draw"}
        game["phase"] = "drawn"
        mp.set_last_action(game, "draw", sid=sid)
        add_log(game, f"{player_name(game, sid)} drew from the deck.")
        return
    if game["discard_pile"]:
        card = game["discard_pile"].pop()
        unlock_discard_card_for_burn(game, card)
        reset_discard_burn_state(game)
        game["pending_draw"] = {"sid": sid, "card": card, "source": "discard"}
        game["phase"] = "drawn"
        mp.set_last_action(game, "take", sid=sid, card=public_card(card))
        add_log(game, f"{player_name(game, sid)} took the last discard.")
        return
    perform_bot_call(game, sid)


def perform_bot_drawn(game, sid):
    pending = game.get("pending_draw")
    if current_sid(game) != sid or game["phase"] != "drawn" or not pending or pending["sid"] != sid:
        return
    config = bot_config(game["players"][sid])
    card = pending["card"]
    index, gain = best_swap_slot(game, sid, card, config)
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
    mp.advance_turn(game)


def bot_play_drawn(game, sid):
    mp = runtime()
    pending = game["pending_draw"]
    card = pending["card"]
    discard_card(game, card)
    game["pending_draw"] = None
    mp.set_last_action(game, "play", sid=sid, card=public_card(card))
    add_log(game, f"{player_name(game, sid)} played {card['label']} to discard.")
    player = game["players"][sid]
    if card["ability"] and random.random() < bot_config(player)["ability_rate"]:
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
    if (
        peek.get("burnable")
        and game["discard_pile"]
        and not is_discard_burn_locked(game)
        and burn_matches(game["discard_pile"][-1], peek["card"])
        and random.random() < bot_config(game["players"][sid])["peek_burn_rate"]
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
            index, _ = max(own_slots, key=lambda item: item[1]["card"]["value"])
            mp.begin_held_peek(game, sid, sid, index)
        else:
            game["pending_ability"] = None
            mp.advance_turn(game)
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
    config = bot_config(game["players"][sid])
    slots = all_switchable_slots(game, sid)
    if len(slots) < 2:
        return None
    if random.random() < config["switch_random_rate"]:
        first, second = random.sample(slots, 2)
        return (
            (first[0], first[1]),
            (second[0], second[1]),
            random.random() < config["switch_execute_rate"],
        )
    own_slots = [(sid, index, slot) for index, slot in live_slots_for(game, sid)]
    opponent_slots = [item for item in slots if item[0] != sid]
    if not own_slots or not opponent_slots:
        first, second = random.sample(slots, 2)
        return (
            (first[0], first[1]),
            (second[0], second[1]),
            can_peek and random.random() < config["switch_execute_rate"],
        )
    own_high = max(own_slots, key=lambda item: item[2]["card"]["value"])
    if random.random() < config["switch_target_lowest"]:
        opponent = min(opponent_slots, key=lambda item: item[2]["card"]["value"])
        should_switch = own_high[2]["card"]["value"] > opponent[2]["card"]["value"]
    else:
        opponent = random.choice(opponent_slots)
        should_switch = own_high[2]["card"]["value"] >= config["switch_own_min"]
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
    if not game["first_caller_sid"]:
        game["first_caller_sid"] = sid
        game["final_turns_remaining"] = [
            player_sid for player_sid in game["player_order"] if player_sid != sid
        ]
        add_log(game, f"{player_name(game, sid)} called. Everyone else gets one final turn.")
    else:
        add_log(game, f"{player_name(game, sid)} called to protect their cards.")
    mp.advance_turn(game)
