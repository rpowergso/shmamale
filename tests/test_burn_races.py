from copy import deepcopy
import unittest
from unittest.mock import patch

from app import app, socketio
from bot import BOT_CONFIG, initialize_bot_round_knowledge, maybe_schedule_bot_burn
from game import make_card, make_player, make_slot, new_room, reset_discard_burn_state
from multiplayer import (
    advance_turn,
    apply_failed_burn,
    finalize_burn_contest,
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

    def test_same_slot_stays_live_until_server_resolves_showdown(self):
        game, discard = self.make_race_game()
        target = card("7", "diamonds", deck_number=2)
        game["players"]["human"] = make_player("Human")
        game["players"]["human"]["board"] = [make_slot(target), None, None, None]
        game["player_order"].append("human")
        started = game["burn_window_started_at"]

        first, _, _ = resolve_burn_attempt(
            game, "bot1", "human", 0, discard["id"], started + 1.1
        )
        second, _, _ = resolve_burn_attempt(
            game, "bot2", "human", 0, discard["id"], started + 1.35
        )

        self.assertEqual(first, "pending")
        self.assertEqual(second, "pending")
        self.assertEqual(game["players"]["human"]["board"][0]["card"]["id"], target["id"])
        self.assertIsNone(game["burn_showdown"])

        self.assertTrue(finalize_burn_contest(game, discard["id"]))
        self.assertEqual(game["burn_showdown"]["winner_sid"], "bot1")
        self.assertEqual(len(game["burn_showdown"]["attempts"]), 2)
        loser = game["burn_showdown"]["attempts"][1]
        self.assertEqual(loser["result"], "late")
        self.assertTrue(loser["penalty"])
        self.assertEqual(loser["delta_ms"], 250)
        self.assertEqual(game["players"]["bot2"]["board"][0]["card"]["rank"], "5")
        self.assertEqual(game["pending_burn"]["sid"], "bot1")
        self.assertIsNone(game["players"]["human"]["board"][0])

    def test_single_burn_is_delayed_and_names_the_burner(self):
        game, discard = self.make_race_game()
        started = game["burn_window_started_at"]
        outcome, _, _ = resolve_burn_attempt(
            game, "bot1", "bot1", 2, discard["id"], started + 0.9
        )
        self.assertEqual(outcome, "pending")
        self.assertIsNotNone(game["players"]["bot1"]["board"][2])

        finalize_burn_contest(game, discard["id"])
        self.assertEqual(game["burn_showdown"]["winner_sid"], "bot1")
        self.assertEqual(game["burn_showdown"]["attempts"][0]["delta_ms"], 0)
        self.assertIsNone(game["players"]["bot1"]["board"][2])

    def test_attempt_after_server_cutoff_cannot_enter_showdown(self):
        game, discard = self.make_race_game()
        started = game["burn_window_started_at"]
        first, _, _ = resolve_burn_attempt(
            game, "bot1", "bot1", 2, discard["id"], started + 0.5
        )
        late, _, message = resolve_burn_attempt(
            game, "bot2", "bot2", 2, discard["id"], started + 1.36
        )

        self.assertEqual(first, "pending")
        self.assertEqual(late, "error")
        self.assertIn("window has closed", message)
        finalize_burn_contest(game, discard["id"])
        self.assertEqual(len(game["burn_showdown"]["attempts"]), 1)

    def test_discard_remains_burnable_after_an_all_miss_showdown(self):
        game, discard = self.make_race_game()
        game["players"]["bot1"]["board"][2] = make_slot(card("8", "clubs"))
        started = game["burn_window_started_at"]
        first, _, _ = resolve_burn_attempt(
            game, "bot1", "bot1", 2, discard["id"], started + 0.5
        )
        self.assertEqual(first, "pending")
        finalize_burn_contest(game, discard["id"])
        self.assertIsNone(game["burn_showdown"]["winner_sid"])

        second, _, _ = resolve_burn_attempt(
            game, "bot2", "bot2", 2, discard["id"], started + 1.6
        )
        self.assertEqual(second, "pending")

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


class LiveBurnShowdownTests(unittest.TestCase):
    def tearDown(self):
        rooms.pop("LIVEBURN", None)

    def test_two_clients_can_target_same_slot_before_shared_resolution(self):
        first = socketio.test_client(app)
        second = socketio.test_client(app)
        try:
            first.emit("join", {"room": "LIVEBURN", "username": "First"})
            second.emit("join", {"room": "LIVEBURN", "username": "Second"})
            game = rooms["LIVEBURN"]
            first_sid = next(
                sid for sid, player in game["players"].items()
                if player["username"] == "First"
            )
            second_sid = next(
                sid for sid, player in game["players"].items()
                if player["username"] == "Second"
            )
            target = card("7", "spades")
            discard = card("7", "hearts")
            game["status"] = "playing"
            game["phase"] = "choose"
            game["players"][first_sid]["board"] = [make_slot(card("3")), None, None, None]
            game["players"][second_sid]["board"] = [make_slot(target), None, None, None]
            game["discard_pile"] = [discard]
            game["draw_pile"] = [card("4", "diamonds")]
            reset_discard_burn_state(game)
            first.get_received()
            second.get_received()

            payload = {
                "room": "LIVEBURN",
                "owner_sid": second_sid,
                "index": 0,
                "discard_id": discard["id"],
            }
            first.emit("burn_card", payload)
            socketio.sleep(0.06)
            second.emit("burn_card", payload)

            first_packets = first.get_received()
            second_packets = second.get_received()
            self.assertTrue(any(p["name"] == "burn_attempt_registered" for p in first_packets))
            self.assertTrue(any(p["name"] == "burn_attempt_registered" for p in second_packets))
            self.assertFalse(any(p["name"] == "game_state" for p in first_packets))
            self.assertEqual(
                game["players"][second_sid]["board"][0]["card"]["id"],
                target["id"],
            )

            socketio.sleep(1.0)
            final_states = [
                packet["args"][0]
                for packet in second.get_received()
                if packet["name"] == "game_state"
            ]
            self.assertTrue(final_states)
            showdown = final_states[-1]["burn_showdown"]
            self.assertEqual(showdown["winner_sid"], first_sid)
            self.assertEqual(len(showdown["attempts"]), 2)
            self.assertGreaterEqual(showdown["attempts"][1]["delta_ms"], 40)
        finally:
            first.disconnect()
            second.disconnect()


if __name__ == "__main__":
    unittest.main()
