import json
import unittest

from app import app, socketio
from bot import BOT_CONFIG
from game import (
    build_deck,
    can_opening_peek_slot,
    default_settings,
    make_card,
    make_player,
    make_slot,
    new_room,
)
from multiplayer import finish_round, rooms, start_round
from multiplayer import player_view


def card(rank, suit="clubs", deck_number=1):
    return make_card(rank, suit=suit, deck_number=deck_number)


class CustomGridTests(unittest.TestCase):
    def make_game(self):
        game = new_room("a")
        game["players"] = {
            "a": make_player("A"),
            "b": make_player("B"),
            "c": make_player("C"),
        }
        game["player_order"] = ["a", "b", "c"]
        game["status"] = "playing"
        return game

    def test_default_preset_matches_requested_rules(self):
        settings = default_settings()
        self.assertEqual(settings["target_score"], 50)
        self.assertEqual(settings["win_condition"], "last_standing")
        self.assertEqual((settings["grid_rows"], settings["grid_cols"]), (2, 2))
        self.assertEqual(settings["grid_peek_modes"], ["none", "none", "self", "self"])
        self.assertEqual(settings["joker_value"], -2)

    def test_rectangular_grid_deals_requested_number_of_cards(self):
        game = self.make_game()
        game["status"] = "lobby"
        game["settings"].update(
            {
                "grid_rows": 2,
                "grid_cols": 3,
                "grid_peek_modes": ["none"] * 6,
            }
        )
        start_round(game)
        self.assertTrue(all(len(player["board"]) == 6 for player in game["players"].values()))

    def test_maximum_four_by_four_grid_deals_sixteen_cards(self):
        game = self.make_game()
        game["status"] = "lobby"
        game["settings"].update(
            {
                "grid_rows": 4,
                "grid_cols": 4,
                "grid_peek_modes": ["none"] * 16,
            }
        )
        start_round(game)
        self.assertTrue(all(len(player["board"]) == 16 for player in game["players"].values()))
        self.assertGreater(len(game["draw_pile"]), 0)

    def test_joker_value_zero_is_applied_to_the_deck(self):
        deck = build_deck(deck_count=1, jokers=2, joker_value=0)
        jokers = [item for item in deck if item["rank"] == "JOKER"]
        self.assertEqual([item["value"] for item in jokers], [0, 0])

    def test_opening_peek_modes_are_mutually_exclusive_and_seat_aware(self):
        game = self.make_game()
        game["settings"].update(
            {
                "grid_peek_modes": ["self", "all_opponents", "seat_opponent", "none"],
                "opponent_peek_distance": 1,
                "opponent_peek_direction": "right",
            }
        )
        self.assertTrue(can_opening_peek_slot(game, "a", "a", 0))
        self.assertFalse(can_opening_peek_slot(game, "a", "b", 0))
        self.assertTrue(can_opening_peek_slot(game, "a", "b", 1))
        self.assertTrue(can_opening_peek_slot(game, "a", "b", 2))
        self.assertFalse(can_opening_peek_slot(game, "a", "c", 2))
        self.assertFalse(can_opening_peek_slot(game, "a", "a", 3))

    def test_opening_peek_reveals_only_to_the_viewer(self):
        game = self.make_game()
        game["players"]["a"]["board"] = [make_slot(card("K", "hearts"))]
        game["players"]["b"]["board"] = [make_slot(card("A"))]
        game["settings"]["grid_peek_modes"] = ["all_opponents"]
        game["players"]["a"]["opening_peeked"].add("b:0")

        a_view = player_view(game, "a")
        c_view = player_view(game, "c")
        self.assertTrue(a_view["players"]["b"]["board"][0]["faceUp"])
        self.assertEqual(a_view["players"]["b"]["board"][0]["card"]["value"], 1)
        self.assertFalse(c_view["players"]["b"]["board"][0]["faceUp"])
        self.assertIsNone(c_view["players"]["b"]["board"][0]["card"])

        game["settings"]["grid_peek_modes"] = ["none"]
        no_peek_view = player_view(game, "a")
        self.assertFalse(no_peek_view["players"]["a"]["opening_peekable"])

    def test_black_king_targets_are_public_without_revealing_cards(self):
        game = self.make_game()
        game["phase"] = "ability"
        game["players"]["b"]["board"][0] = make_slot(card("K", "hearts"))
        game["pending_ability"] = {
            "sid": "a",
            "type": "switch_peek",
            "stage": "selecting",
            "selected": [{"owner_sid": "b", "index": 0}],
            "inspected": [{"owner_sid": "b", "index": 0}],
            "inspection_count": 1,
        }

        spectator_view = player_view(game, "c")
        ability = spectator_view["pending_ability"]
        self.assertEqual(ability["targets"], [{"owner_sid": "b", "index": 0}])
        self.assertEqual(ability["selected"], [])
        self.assertIsNone(spectator_view["players"]["b"]["board"][0]["card"])


class WinConditionTests(unittest.TestCase):
    def make_scoring_game(self, condition):
        game = new_room("a")
        game["status"] = "playing"
        game["phase"] = "choose"
        game["round_number"] = 1
        game["settings"]["win_condition"] = condition
        game["players"] = {
            "a": make_player("A"),
            "b": make_player("B"),
            "c": make_player("C"),
        }
        game["player_order"] = ["a", "b", "c"]
        game["players"]["a"]["score"] = 49
        game["players"]["a"]["board"] = [make_slot(card("2"))]
        game["players"]["b"]["board"] = [make_slot(card("3"))]
        game["players"]["c"]["board"] = [make_slot(card("4"))]
        return game

    def test_last_standing_eliminates_busted_player_into_spectating(self):
        game = self.make_scoring_game("last_standing")
        finish_round(game)
        self.assertEqual(game["status"], "round_over")
        self.assertTrue(game["players"]["a"]["eliminated"])
        self.assertTrue(game["players"]["a"]["spectating"])
        self.assertEqual(game["round_results"]["eliminated"], ["a"])

        start_round(game)
        self.assertTrue(all(slot is None for slot in game["players"]["a"]["board"]))
        self.assertIn(game["player_order"][game["turn_index"]], {"b", "c"})

    def test_first_bust_mode_awards_win_to_lowest_total(self):
        game = self.make_scoring_game("first_bust_lowest")
        finish_round(game)
        self.assertEqual(game["status"], "game_over")
        self.assertEqual(game["winner_summary"]["winners"], ["b"])

    def test_last_survivor_wins_and_eliminated_player_is_not_rescored(self):
        game = self.make_scoring_game("last_standing")
        game["players"]["b"]["eliminated"] = True
        game["players"]["b"]["spectating"] = True
        game["players"]["c"]["score"] = 45
        previous_b_score = game["players"]["b"]["score"]

        finish_round(game)

        self.assertEqual(game["status"], "game_over")
        self.assertEqual(game["winner_summary"]["winners"], ["c"])
        self.assertEqual(game["players"]["b"]["score"], previous_b_score)


class UnifiedLobbySocketTests(unittest.TestCase):
    def tearDown(self):
        rooms.pop("LOBBY", None)
        rooms.pop("START", None)

    def test_host_can_customize_room_and_manage_individual_bots(self):
        client = socketio.test_client(app)
        try:
            client.emit("join", {"room": "LOBBY", "username": "Host"})
            client.emit(
                "update_settings",
                {
                    "room": "LOBBY",
                    "target_score": 75,
                    "win_condition": "first_bust_lowest",
                    "grid_rows": 3,
                    "grid_cols": 2,
                    "grid_peek_modes": [
                        "self",
                        "none",
                        "all_opponents",
                        "seat_opponent",
                        "none",
                        "self",
                    ],
                    "opponent_peek_distance": 2,
                    "opponent_peek_direction": "right",
                    "joker_value": 0,
                },
            )
            client.emit("add_bot", {"room": "LOBBY", "difficulty": "custom"})

            game = rooms["LOBBY"]
            bot_sid = next(sid for sid in game["player_order"] if sid.startswith("BOT:"))
            self.assertEqual(game["settings"]["target_score"], 75)
            self.assertEqual((game["settings"]["grid_rows"], game["settings"]["grid_cols"]), (3, 2))
            self.assertEqual(game["settings"]["joker_value"], 0)
            self.assertEqual(game["settings"]["deck_count"], 1)
            self.assertEqual(game["players"][bot_sid]["difficulty"], "custom")
            self.assertEqual(
                game["players"][bot_sid]["bot_policy"]["reaction"],
                BOT_CONFIG["medium"]["reaction"],
            )

            client.emit(
                "update_bot_difficulty",
                {"room": "LOBBY", "sid": bot_sid, "difficulty": "hard"},
            )
            self.assertEqual(game["players"][bot_sid]["difficulty"], "hard")
            client.emit("remove_bot", {"room": "LOBBY", "sid": bot_sid})
            self.assertNotIn(bot_sid, game["players"])

            client.emit(
                "update_settings",
                {
                    "room": "LOBBY",
                    "grid_rows": 99,
                    "grid_cols": 1,
                    "grid_peek_modes": ["self", "invalid"],
                },
            )
            self.assertEqual((game["settings"]["grid_rows"], game["settings"]["grid_cols"]), (4, 2))
            self.assertEqual(len(game["settings"]["grid_peek_modes"]), 8)
            self.assertEqual(game["settings"]["grid_peek_modes"][:2], ["self", "none"])

            client.emit("update_settings", {"room": "LOBBY", "preset": "default"})
            self.assertEqual(game["settings"], default_settings())
        finally:
            client.disconnect()

    def test_unified_room_starts_custom_game_with_an_added_bot(self):
        client = socketio.test_client(app)
        try:
            client.emit("join", {"room": "START", "username": "Host"})
            client.emit(
                "update_settings",
                {
                    "room": "START",
                    "grid_rows": 2,
                    "grid_cols": 3,
                    "grid_peek_modes": [
                        "none",
                        "none",
                        "none",
                        "self",
                        "self",
                        "self",
                    ],
                    "joker_value": 0,
                    "deck_count": 1,
                },
            )
            client.emit("add_bot", {"room": "START", "difficulty": "easy"})
            client.emit("start_game", {"room": "START"})

            game = rooms["START"]
            self.assertEqual(game["status"], "playing")
            self.assertEqual(len(game["players"]), 2)
            self.assertTrue(all(len(player["board"]) == 6 for player in game["players"].values()))
            dealt_cards = [
                slot["card"]
                for player in game["players"].values()
                for slot in player["board"]
                if slot
            ]
            jokers = [
                item
                for item in game["draw_pile"] + dealt_cards
                if item["rank"] == "JOKER"
            ]
            self.assertEqual(len(jokers), 2)
            self.assertTrue(all(item["value"] == 0 for item in jokers))
            self.assertEqual(len(game["draw_pile"]) + len(dealt_cards), 54)
        finally:
            client.disconnect()

    def test_room_rejects_a_grid_that_cannot_fit_in_selected_decks(self):
        client = socketio.test_client(app)
        try:
            client.emit("join", {"room": "START", "username": "Host"})
            for _ in range(3):
                client.emit("add_bot", {"room": "START", "difficulty": "medium"})
            client.emit(
                "update_settings",
                {
                    "room": "START",
                    "grid_rows": 4,
                    "grid_cols": 4,
                    "grid_peek_modes": ["none"] * 16,
                    "deck_count": 1,
                },
            )
            client.emit("start_game", {"room": "START"})

            self.assertEqual(rooms["START"]["status"], "lobby")
            errors = [
                packet["args"][0]["msg"]
                for packet in client.get_received()
                if packet["name"] == "error_message"
            ]
            self.assertTrue(any("more decks" in message for message in errors))
        finally:
            client.disconnect()

    def test_human_opponents_still_have_to_ready_up(self):
        host = socketio.test_client(app)
        friend = socketio.test_client(app)
        try:
            host.emit("join", {"room": "START", "username": "Host"})
            friend.emit("join", {"room": "START", "username": "Friend"})
            host.emit("start_game", {"room": "START"})
            self.assertEqual(rooms["START"]["status"], "lobby")

            host.emit("toggle_ready", {"room": "START"})
            friend.emit("toggle_ready", {"room": "START"})
            host.emit("start_game", {"room": "START"})
            self.assertEqual(rooms["START"]["status"], "playing")
        finally:
            host.disconnect()
            friend.disconnect()


class LiveChatSocketTests(unittest.TestCase):
    def tearDown(self):
        rooms.pop("CHAT", None)

    def test_room_chat_broadcasts_persists_and_rate_limits(self):
        host = socketio.test_client(app)
        friend = socketio.test_client(app)
        late_joiner = None
        try:
            host.emit("join", {"room": "CHAT", "username": "Host"})
            friend.emit("join", {"room": "CHAT", "username": "Friend"})
            host.get_received()
            friend.get_received()

            host.emit("send_chat", {"room": "CHAT", "message": "  hello   everyone  "})
            host_messages = [
                packet["args"][0]
                for packet in host.get_received()
                if packet["name"] == "chat_message"
            ]
            friend_messages = [
                packet["args"][0]
                for packet in friend.get_received()
                if packet["name"] == "chat_message"
            ]
            self.assertEqual(host_messages[-1]["message"], "hello everyone")
            self.assertEqual(friend_messages[-1]["username"], "Host")

            host.emit("send_chat", {"room": "CHAT", "message": "too fast"})
            errors = [
                packet["args"][0]["msg"]
                for packet in host.get_received()
                if packet["name"] == "error_message"
            ]
            self.assertTrue(any("wait a moment" in message for message in errors))

            friend.emit("send_chat", {"room": "CHAT", "message": "x" * 300})
            self.assertEqual(len(rooms["CHAT"]["chat_messages"][-1]["message"]), 240)

            late_joiner = socketio.test_client(app)
            late_joiner.emit("join", {"room": "CHAT", "username": "Late"})
            histories = [
                packet["args"][0]["messages"]
                for packet in late_joiner.get_received()
                if packet["name"] == "chat_history"
            ]
            self.assertEqual([item["message"] for item in histories[-1]], [
                "hello everyone",
                "x" * 240,
            ])
        finally:
            host.disconnect()
            friend.disconnect()
            if late_joiner and late_joiner.is_connected():
                late_joiner.disconnect()


class SocketReconnectTests(unittest.TestCase):
    def tearDown(self):
        rooms.pop("REJOIN", None)

    def test_disconnected_player_reclaims_live_game_with_stable_token(self):
        first = socketio.test_client(app)
        second = None
        try:
            first.emit(
                "join",
                {
                    "room": "REJOIN",
                    "username": "Host",
                    "reconnect_token": "stable-browser-token",
                },
            )
            game = rooms["REJOIN"]
            old_sid = game["player_order"][0]
            game["status"] = "playing"
            game["phase"] = "drawn"
            game["current_turn_sid"] = old_sid
            game["pending_draw"] = {
                "sid": old_sid,
                "card": card("4"),
                "source": "draw",
            }
            game["burnt_slots"] = [f"{old_sid}:0"]

            first.disconnect()
            self.assertFalse(game["players"][old_sid]["connected"])

            second = socketio.test_client(app)
            second.emit(
                "join",
                {
                    "room": "REJOIN",
                    "username": "Host",
                    "reconnect_token": "stable-browser-token",
                },
            )

            game = rooms["REJOIN"]
            new_sid = game["player_order"][0]
            self.assertNotEqual(new_sid, old_sid)
            self.assertNotIn(old_sid, game["players"])
            self.assertEqual(game["host_sid"], new_sid)
            self.assertEqual(game["current_turn_sid"], new_sid)
            self.assertEqual(game["pending_draw"]["sid"], new_sid)
            self.assertEqual(game["burnt_slots"], [f"{new_sid}:0"])
            self.assertTrue(game["players"][new_sid]["connected"])
            states = [
                packet["args"][0]
                for packet in second.get_received()
                if packet["name"] == "game_state"
            ]
            self.assertTrue(states)
            self.assertEqual(states[-1]["viewer_sid"], new_sid)
        finally:
            if first.is_connected():
                first.disconnect()
            if second and second.is_connected():
                second.disconnect()


class UnifiedRoomRouteTests(unittest.TestCase):
    def test_home_and_legacy_bot_routes_lead_to_unified_rooms(self):
        client = app.test_client()
        homepage = client.get("/homepage")
        self.assertEqual(homepage.status_code, 200)
        self.assertIn(b"CREATE PLAY ROOM", homepage.data)
        self.assertIn(b"HOW TO PLAY", homepage.data)
        self.assertIn(b'href="/tutorial"', homepage.data)
        self.assertNotIn(b"VS BOTS", homepage.data)

        health = client.get("/health")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.get_json(), {"status": "ok"})

        tutorial = client.get("/tutorial")
        self.assertEqual(tutorial.status_code, 200)
        self.assertEqual(tutorial.data.count(b"data-tutorial-step="), 9)
        self.assertIn(b"Take a look at the keybinds", tutorial.data)
        self.assertIn(b"HIGHLIGHT COLORS", tutorial.data)
        self.assertIn(b"keyboard focus", tutorial.data)
        self.assertIn(b"7, 8", tutorial.data)
        self.assertIn(b"see your fate", tutorial.data)
        self.assertIn(b"9, 10", tutorial.data)
        self.assertIn(b"look at a friend", tutorial.data)
        self.assertIn(b"Jack, Queen", tutorial.data)
        self.assertIn(b"switch unseen", tutorial.data)
        self.assertIn(b"Black King", tutorial.data)
        self.assertIn(b"switch looking", tutorial.data)

        legacy = client.get("/botgamescreen")
        self.assertEqual(legacy.status_code, 302)
        self.assertTrue(legacy.headers["Location"].endswith("/create-room"))

        room = client.get("/multiplayer/abcd")
        self.assertEqual(room.status_code, 200)
        self.assertIn(b'id="grid-rule-editor"', room.data)
        self.assertIn(b'id="add-bot-btn"', room.data)
        self.assertIn(b'id="setting-deck-count"', room.data)
        self.assertIn(b'id="keybinds-overlay"', room.data)
        self.assertIn(b'id="keyboard-action-menu"', room.data)
        self.assertIn(b'id="room-chat"', room.data)
        self.assertIn(b'id="chat-form"', room.data)
        self.assertIn(b"Rotate sideways for the best table view", room.data)
        self.assertIn(b"INVITE CODE", room.data)

        with app.open_resource("static/css/style.css") as css_file:
            css = css_file.read()
        self.assertIn(b".grid-rule-cell.chosen", css)
        self.assertIn(b".board-card.keyboard-focus", css)
        self.assertIn(b"orientation: landscape", css)
        self.assertIn(b".orientation-tip", css)

        with app.open_resource("static/js/shmamale.js") as js_file:
            game_js = js_file.read()
        self.assertIn(b"function handleGameKeydown", game_js)
        self.assertIn(b"selectKeyboardPlayer(Number(event.key))", game_js)
        self.assertIn(b'event.code === "Space"', game_js)
        self.assertIn(b'event.key === "Enter"', game_js)
        self.assertIn(b'"(pointer: coarse)"', game_js)
        self.assertIn(b'window.visualViewport?.addEventListener("resize"', game_js)
        self.assertIn(b"compactLandscape ? measuredHeight", game_js)
        self.assertIn(b'socket.emit("send_chat"', game_js)
        self.assertIn(b"escapeHtml(message.message", game_js)
        self.assertIn(b'"burn_attempt_registered"', game_js)
        self.assertIn(b"king-targeted", game_js)
        self.assertIn(b"delta_ms", game_js)

        self.assertIn(b".board-card.king-targeted", css)
        self.assertIn(b".board-card.burn-attempt-pending", css)

        with app.open_resource("railway.json") as railway_file:
            railway_config = json.load(railway_file)
        start_command = railway_config["deploy"]["startCommand"]
        self.assertIn("--worker-class gthread", start_command)
        self.assertIn("--workers 1", start_command)
        self.assertIn("${PORT:-8000}", start_command)
        self.assertEqual(railway_config["deploy"]["healthcheckPath"], "/health")
        self.assertNotIn(b".grid-rule-cell.seat", css)


if __name__ == "__main__":
    unittest.main()
