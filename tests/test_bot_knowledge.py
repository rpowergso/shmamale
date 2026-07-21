import random
import unittest
from unittest.mock import patch

from bot import (
    BOT_CONFIG,
    BOT_DIFFICULTIES,
    UNKNOWN_CARD_VALUE,
    bot_pressure,
    bot_should_call,
    bot_should_play_ability,
    choose_bot_burn_candidate,
    configure_bots,
    estimated_board_score,
    estimated_player_board_score,
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
        self.assertLessEqual(medium["mistake"], 0.01)
        self.assertLessEqual(medium["reaction"][1], 1.6)
        self.assertGreaterEqual(medium["call_score"], 6)
        self.assertGreaterEqual(medium["ability_rate"], 0.99)

    def test_hard_is_materially_stronger_than_medium(self):
        medium = BOT_CONFIG["medium"]
        hard = BOT_CONFIG["hard"]
        self.assertLess(hard["reaction"][1], medium["reaction"][0])
        self.assertLessEqual(hard["memory_error"], medium["memory_error"])
        self.assertLessEqual(hard["mistake"], medium["mistake"])
        self.assertGreater(hard["switch_peek_value"], medium["switch_peek_value"])

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


class SweatDifficultyTests(unittest.TestCase):
    def make_game(self, hidden_rank="K", hidden_suit="clubs"):
        game = new_room("human")
        game["status"] = "playing"
        game["players"] = {
            "human": make_player("Human"),
            "bot": make_player(
                "Sweat Bot",
                is_bot=True,
                difficulty="sweat",
                bot_policy=deepcopy(BOT_CONFIG["sweat"]),
            ),
        }
        game["player_order"] = ["human", "bot"]
        game["players"]["human"]["board"] = [
            make_slot(card(hidden_rank, hidden_suit)) for _ in range(4)
        ]
        game["players"]["bot"]["board"] = [
            make_slot(card("2")),
            make_slot(card("2", "spades")),
            make_slot(card("3")),
            make_slot(card("3", "spades")),
        ]
        for index in range(4):
            remember_bot_card(game, "bot", "bot", index)
        return game

    def test_sweat_is_available_instant_and_error_free(self):
        sweat = BOT_CONFIG["sweat"]
        self.assertIn("sweat", BOT_DIFFICULTIES)
        self.assertTrue(sweat["instant_actions"])
        self.assertLessEqual(sweat["reaction"][1], 0.04)
        self.assertEqual(sweat["mistake"], 0)
        self.assertEqual(sweat["memory_error"], 0)
        self.assertEqual(sweat["burn_miss_rate"], 0)

    def test_sweat_estimates_hidden_cards_instead_of_reading_them(self):
        high_game = self.make_game("K", "clubs")
        low_game = self.make_game("K", "hearts")

        high_estimate = estimated_player_board_score(high_game, "bot", "human")
        low_estimate = estimated_player_board_score(low_game, "bot", "human")
        self.assertEqual(high_estimate, low_estimate)
        self.assertEqual(high_estimate, UNKNOWN_CARD_VALUE * 4)
        self.assertTrue(all(
            known_card_info(high_game, "bot", "human", index) is None
            for index in range(4)
        ))

    def test_sweat_values_a_large_swap_over_a_small_special(self):
        game = self.make_game()
        seven = card("7")
        black_king = card("K", "clubs")
        with patch("bot.random.random", return_value=0.5):
            self.assertFalse(bot_should_play_ability(game, "bot", seven, 12))
            self.assertTrue(bot_should_play_ability(game, "bot", black_king, 4))

    def test_sweat_calls_from_a_probable_lead_using_only_visible_information(self):
        game = self.make_game()
        policy = game["players"]["bot"]["bot_policy"]
        policy["call_score"] = -1
        policy["call_card_count"] = 0
        with patch("bot.random.random", return_value=0.5):
            self.assertTrue(bot_should_call(game, "bot"))

        game["players"]["human"]["board"] = [
            {"card": card("A", suit), "revealed": True}
            for suit in ("clubs", "spades", "hearts", "diamonds")
        ]
        with patch("bot.random.random", return_value=0.5):
            self.assertFalse(bot_should_call(game, "bot"))

    def test_sweat_still_will_not_burn_an_unknown_match(self):
        game = self.make_game("7", "hearts")
        top_card = card("7", "clubs")
        self.assertIsNone(choose_bot_burn_candidate(game, top_card, "bot"))


if __name__ == "__main__":
    unittest.main()
