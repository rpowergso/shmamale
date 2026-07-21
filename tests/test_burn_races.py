from copy import deepcopy
import unittest
from unittest.mock import patch

from bot import BOT_CONFIG, initialize_bot_round_knowledge, maybe_schedule_bot_burn
from game import make_card, make_player, make_slot, new_room, reset_discard_burn_state
from multiplayer import (
    advance_turn,
    apply_failed_burn,
    resolve_burn_attempt,
    rooms,
)


def card(rank, suit="clubs", deck_number=1):
    return make_card(rank, suit=suit, deck_number=deck_number)


class BurnRevealTests(unittest.TestCase):
    def test_failed_burn_flips_down_after_the_following_turn(self):
        game = new_room("a")
        game["status"] = "playing"
        game["phase"] = "choose"
        game["players"] = {
            "a": make_player("A"),
            "b": make_player("B"),
            "c": make_player("C"),
        }
        game["player_order"] = ["a", "b", "c"]
        target = card("9", "hearts")
        game["players"]["b"]["board"] = [make_slot(target), None, None, None]
        game["draw_pile"] = [card("3", "spades")]

        apply_failed_burn(game, "c", "b", 0, target, "rank")
        self.assertTrue(game["players"]["b"]["board"][0]["revealed"])

        advance_turn(game)  # Current turn ends; B's following turn begins.
        self.assertTrue(game["players"]["b"]["board"][0]["revealed"])

        advance_turn(game)  # B's turn ends.
        self.assertFalse(game["players"]["b"]["board"][0]["revealed"])


class BurnRaceTests(unittest.TestCase):
    def make_race_game(self, room="RACE"):
        game = new_room("bot1")
        game["room_id"] = room
        game["status"] = "playing"
        game["phase"] = "choose"
        policy = deepcopy(BOT_CONFIG["hard"])
        policy["mistake"] = 0
        policy["burn_miss_rate"] = 0
        game["players"] = {
            "bot1": make_player("Bot One", True, "hard", policy),
            "bot2": make_player("Bot Two", True, "hard", policy),
        }
        game["player_order"] = ["bot1", "bot2"]
        game["players"]["bot1"]["board"] = [
            None,
            None,
            make_slot(card("7", "clubs")),
            None,
        ]
        game["players"]["bot2"]["board"] = [
            None,
            None,
            make_slot(card("7", "spades")),
            None,
        ]
        initialize_bot_round_knowledge(game)
        discard = card("7", "hearts")
        game["discard_pile"] = [discard]
        game["draw_pile"] = [card("4", "diamonds"), card("5", "diamonds")]
        reset_discard_burn_state(game)
        return game, discard

    def test_second_valid_burn_loses_race_and_gets_penalty(self):
        game, discard = self.make_race_game()
        started = game["burn_window_started_at"]

        first, _, _ = resolve_burn_attempt(
            game, "bot1", "bot1", 2, discard["id"], started + 1.1
        )
        second, _, _ = resolve_burn_attempt(
            game, "bot2", "bot2", 2, discard["id"], started + 1.35
        )

        self.assertEqual(first, "success")
        self.assertEqual(second, "race_lost")
        self.assertEqual(game["burn_showdown"]["winner_sid"], "bot1")
        self.assertEqual(len(game["burn_showdown"]["attempts"]), 2)
        loser = game["burn_showdown"]["attempts"][1]
        self.assertEqual(loser["result"], "late")
        self.assertTrue(loser["penalty"])
        self.assertEqual(game["players"]["bot2"]["board"][0]["card"]["rank"], "5")

    def test_each_eligible_bot_gets_an_independent_burn_timer(self):
        room = "SCHEDULE"
        game, _ = self.make_race_game(room)
        rooms[room] = game
        try:
            with patch("bot.socketio.start_background_task") as start_task:
                maybe_schedule_bot_burn(room)
            self.assertEqual(start_task.call_count, 2)
            scheduled_sids = {
                call.args[3]["sid"]
                for call in start_task.call_args_list
            }
            self.assertEqual(scheduled_sids, {"bot1", "bot2"})
        finally:
            rooms.pop(room, None)

    def test_bots_recheck_after_a_failed_burn_reveals_a_new_target(self):
        room = "REVEALED"
        game, discard = self.make_race_game(room)
        game["players"]["bot1"]["board"][2] = make_slot(card("8", "clubs"))
        game["players"]["bot2"]["board"][2] = make_slot(card("8", "spades"))
        game["players"]["human"] = make_player("Human")
        target = card("7", "diamonds", deck_number=2)
        game["players"]["human"]["board"] = [make_slot(target), None, None, None]
        game["player_order"].append("human")
        initialize_bot_round_knowledge(game)
        rooms[room] = game
        try:
            with patch("bot.socketio.start_background_task") as before_reveal:
                maybe_schedule_bot_burn(room)
            self.assertEqual(before_reveal.call_count, 0)

            apply_failed_burn(game, "human", "human", 0, target, "rank")
            with patch("bot.socketio.start_background_task") as after_reveal:
                maybe_schedule_bot_burn(room)
            self.assertEqual(after_reveal.call_count, 2)
            self.assertEqual(game["discard_pile"][0]["id"], discard["id"])
        finally:
            rooms.pop(room, None)


if __name__ == "__main__":
    unittest.main()
