import random


SUITS = {
    "clubs": {"short": "C", "symbol": "♣", "color": "black"},
    "spades": {"short": "S", "symbol": "♠", "color": "black"},
    "hearts": {"short": "H", "symbol": "♥", "color": "red"},
    "diamonds": {"short": "D", "symbol": "♦", "color": "red"},
}

RANKS = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]
BOARD_SIZE = 4
BOTTOM_ROW = {2, 3}


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


def make_card(rank, suit=None, deck_number=1, joker_number=None):
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
            "value": -2,
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


def build_deck(deck_count=1, jokers=2):
    deck = []
    for deck_number in range(1, deck_count + 1):
        for suit in SUITS:
            for rank in RANKS:
                deck.append(make_card(rank, suit=suit, deck_number=deck_number))
        for joker_number in range(1, jokers + 1):
            deck.append(make_card("JOKER", deck_number=deck_number, joker_number=joker_number))
    random.shuffle(deck)
    return deck


def make_slot(card):
    return {"card": card, "revealed": False}


def empty_board():
    return [None for _ in range(BOARD_SIZE)]


def deal_board(deck):
    return [make_slot(deck.pop()) for _ in range(BOARD_SIZE)]


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
