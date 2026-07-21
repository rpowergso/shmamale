import random
import unittest
from unittest.mock import patch

from bot import (
    BOT_CONFIG,
    UNKNOWN_CARD_VALUE,
    bot_pressure,
    bot_should_call,
    choose_bot_burn_candidate,
    configure_bots,
    estimated_board_score,
    initialize_bot_round_knowledge,
    known_card_info,
    remember_bot_card,
    record_bot_event,
    record_bot_round_outcomes,
    strategy_config,
    swap_bot_knowledge,
)
from copy import deepcopy

from game import make_card, make_player, make_slot, new_room, swap_slots
from multiplayer import start_round


def card(rank, suit="clubs"):
    return make_card(rank, suit=suit)


class BotKnowledgeTests(unittest.TestCase):
    def setUp(self):
        random.seed(7)
        self.game = new_room("human")
        self.game["status"] = "playing"
        self.game["players"] = {
            "human": make_player("Human"),
            "bot": make_player("Bot", is_bot=True, difficulty="hard"),
        }
        self.game["player_order"] = ["human", "bot"]
        self.game["players"]["bot"]["board"] = [
            make_slot(card("K", "hearts")),
            make_slot(card("K", "diamonds")),
            make_slot(card("A")),
            make_slot(card("2")),
        ]
        initialize_bot_round_knowledge(self.game)

    def test_bot_only_starts_knowing_its_bottom_two_cards(self):
        self.assertIsNone(known_card_info(self.game, "bot", "bot", 0))
        self.assertIsNone(known_card_info(self.game, "bot", "bot", 1))
        self.assertEqual(known_card_info(self.game, "bot", "bot", 2)["value"], 1)
        self.assertEqual(known_card_info(self.game, "bot", "bot", 3)["value"], 2)

    def test_normal_round_start_initializes_only_legal_opening_knowledge(self):
        game = new_room("human")
        game["room_id"] = "TEST"
        game["players"]["human"] = make_player("Human")
        game["player_order"].append("human")
        configure_bots(game, "TEST", 1, "hard")
        start_round(game)

        sid = "BOT:TEST:1"
        self.assertIsNone(known_card_info(game, sid, sid, 0))
        self.assertIsNone(known_card_info(game, sid, sid, 1))
        self.assertIsNotNone(known_card_info(game, sid, sid, 2))
        self.assertIsNotNone(known_card_info(game, sid, sid, 3))

    def test_unknown_red_kings_are_estimated_not_read(self):
        self.assertEqual(
            estimated_board_score(self.game, "bot"),
            UNKNOWN_CARD_VALUE * 2 + 3,
        )
        self.assertFalse(bot_should_call(self.game, "bot"))

    def test_legitimate_peek_adds_card_to_memory(self):
        hidden_card = self.game["players"]["human"]["board"] = [
            make_slot(card("K", "hearts"))
        ]
        remember_bot_card(self.game, "bot", "human", 0)
        self.assertEqual(
            known_card_info(self.game, "bot", "human", 0)["value"],
            hidden_card[0]["card"]["value"],
        )

    def test_unknown_card_stays_unknown_when_switched_onto_bot_board(self):
        self.game["players"]["human"]["board"] = [
            make_slot(card("K", "hearts"))
        ]
        swap_slots(self.game, "human", 0, "bot", 2)
        swap_bot_knowledge(self.game, "human", 0, "bot", 2)

        self.assertIsNone(known_card_info(self.game, "bot", "bot", 2))
        self.assertEqual(known_card_info(self.game, "bot", "human", 0)["value"], 1)
        self.assertFalse(bot_should_call(self.game, "bot"))

    def test_bot_does_not_burn_an_unknown_matching_card(self):
        policy = deepcopy(BOT_CONFIG["hard"])
        policy["mistake"] = 0
        policy["burn_miss_rate"] = 0
        self.game["players"]["bot"]["bot_policy"] = policy
        top_card = card("K", "spades")

        self.assertIsNone(choose_bot_burn_candidate(self.game, top_card))


class MediumDifficultyTests(unittest.TestCase):
    def make_game(self):
        game = new_room("human")
        game["status"] = "playing"
        game["players"] = {
            "human": make_player("Human"),
            "bot": make_player(
                "Bot",
                is_bot=True,
                difficulty="medium",
                bot_policy=deepcopy(BOT_CONFIG["medium"]),
            ),
        }
        game["player_order"] = ["human", "bot"]
        game["players"]["bot"]["board"] = [
            make_slot(card("A")),
            make_slot(card("2")),
            make_slot(card("A", "spades")),
            make_slot(card("2", "spades")),
        ]
        for index in range(4):
            remember_bot_card(game, "bot", "bot", index)
        return game

    def test_medium_baseline_is_challenging(self):
        medium = BOT_CONFIG["medium"]
        self.assertLessEqual(medium["mistake"], 0.04)
        self.assertLessEqual(medium["reaction"][1], 3.4)
        self.assertGreaterEqual(medium["call_score"], 4)
        self.assertGreaterEqual(medium["ability_rate"], 0.9)

    def test_medium_tightens_strategy_when_badly_trailing(self):
        game = self.make_game()
        game["players"]["human"]["score"] = 2
        game["players"]["bot"]["score"] = 30

        pressure = bot_pressure(game, "bot")
        adaptive = strategy_config(game, "bot")

        self.assertGreaterEqual(pressure, 0.9)
        self.assertLess(adaptive["mistake"], BOT_CONFIG["medium"]["mistake"])
        self.assertLess(adaptive["reaction"][1], BOT_CONFIG["medium"]["reaction"][1])
        self.assertGreater(adaptive["call_score"], BOT_CONFIG["medium"]["call_score"])
        with patch("bot.random.random", return_value=0.5):
            self.assertTrue(bot_should_call(game, "bot"))

    def test_medium_does_not_make_a_call_that_would_bust_it(self):
        game = self.make_game()
        game["players"]["human"]["score"] = 2
        game["players"]["bot"]["score"] = 45

        with patch("bot.random.random", return_value=0.5):
            self.assertFalse(bot_should_call(game, "bot"))

    def test_bot_telemetry_stays_in_the_room_and_records_rounds(self):
        game = self.make_game()
        record_bot_event(game, "bot", "draw")
        game["players"]["bot"]["score"] = 9
        record_bot_round_outcomes(game, {"bot": 9}, {"bot": 9})

        telemetry = game["players"]["bot"]["bot_telemetry"]
        self.assertEqual(telemetry["rounds"][-1]["points"], 9)
        self.assertEqual(game["bot_match_log"][0]["event"], "draw")
        self.assertEqual(game["bot_match_log"][-1]["event"], "round_result")


if __name__ == "__main__":
    unittest.main()
