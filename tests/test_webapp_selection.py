import tempfile
import textwrap
import unittest
from pathlib import Path

import json

from tools.webapp.selection import (
    SelectionError,
    extract_agent_code_from_ipynb,
    list_user_decks,
    load_custom_agent,
    parse_decklist_comments,
    parse_deck_csv_text,
    read_deck_csv_file,
    read_user_deck,
    save_user_deck,
    validate_deck_for_builder,
)

REFERENCE_NOTEBOOK = (
    Path(__file__).resolve().parents[1]
    / "data" / "reference" / "kaggle_notebooks"
    / "a-sample-rule-based-agent-mega-lucario-ex-deck.ipynb"
)


class NotebookAgentTests(unittest.TestCase):
    def test_extract_writefile_cell_strips_magic_line(self):
        nb = {"cells": [
            {"cell_type": "markdown", "source": ["# title"]},
            {"cell_type": "code", "source": ["%%writefile main.py\n", "def agent(o):\n", "    return [0]\n"]},
            {"cell_type": "code", "source": ["import tarfile  # packaging only"]},
        ]}
        code = extract_agent_code_from_ipynb(json.dumps(nb))
        self.assertNotIn("%%writefile", code)
        self.assertNotIn("tarfile", code)
        self.assertIn("def agent(o):", code)

    def test_parse_decklist_comments_recovers_60_cards(self):
        code = "A = 673  # ×2\nB = 6  # ×13\n"  # not 60 -> None
        self.assertIsNone(parse_decklist_comments(code))

    @unittest.skipUnless(REFERENCE_NOTEBOOK.exists(), "reference notebook not present")
    def test_reference_notebook_yields_agent_and_60_card_deck(self):
        text = REFERENCE_NOTEBOOK.read_text(encoding="utf-8")
        code = extract_agent_code_from_ipynb(text)
        self.assertIn("def agent", code)
        deck = parse_decklist_comments(code)
        self.assertIsNotNone(deck)
        self.assertEqual(len(deck), 60)
        # たねポケモン(Makuhita=673 など)と基本闘エネルギー(6)を含む
        self.assertIn(673, deck)
        self.assertEqual(deck.count(6), 13)


class DeckCsvTests(unittest.TestCase):
    def test_parse_deck_csv_text_accepts_one_card_id_per_line(self):
        deck = parse_deck_csv_text("\n".join(str(i) for i in range(60)))

        self.assertEqual(deck, list(range(60)))

    def test_parse_deck_csv_text_accepts_comma_separated_csv_cells(self):
        deck = parse_deck_csv_text(",".join(str(i) for i in range(60)))

        self.assertEqual(deck, list(range(60)))

    def test_parse_deck_csv_text_rejects_non_60_card_deck(self):
        with self.assertRaisesRegex(SelectionError, "60"):
            parse_deck_csv_text("\n".join(str(i) for i in range(59)))

    def test_read_deck_csv_file_accepts_utf8_sig(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "deck.csv"
            path.write_text("\ufeff" + "\n".join(str(i) for i in range(60)), encoding="utf-8")

            self.assertEqual(read_deck_csv_file(path), list(range(60)))


class UserDeckTests(unittest.TestCase):
    def test_validate_deck_for_builder_accepts_60_valid_cards_with_basic_pokemon(self):
        deck = [151] * 4 + [152] * 4 + [666] * 4 + [2] * 48

        validate_deck_for_builder(deck)

    def test_validate_deck_for_builder_rejects_cards_outside_pdf_card_pool(self):
        deck = [151] + [2] * 58 + [1268]

        with self.assertRaisesRegex(SelectionError, "使用可能"):
            validate_deck_for_builder(deck)

    def test_save_and_list_user_deck_round_trips_safely_named_csv(self):
        deck = [151] * 4 + [152] * 4 + [666] * 4 + [2] * 48
        with tempfile.TemporaryDirectory() as td:
            saved = save_user_deck("炎 デッキ!/../bad", deck, decks_dir=Path(td))

            self.assertTrue(saved["id"].startswith("fire_"))
            self.assertEqual(read_user_deck(saved["id"], decks_dir=Path(td)), deck)
            listed = list_user_decks(decks_dir=Path(td))
            self.assertEqual(listed[0]["id"], saved["id"])
            self.assertEqual(listed[0]["name"], "炎 デッキ!/../bad")


class CustomAgentTests(unittest.TestCase):
    def test_load_custom_agent_loads_agent_function_from_python_file(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "main.py"
            path.write_text(
                textwrap.dedent(
                    """
                    def agent(obs):
                        return [1, 2, 3]
                    """
                ),
                encoding="utf-8",
            )

            agent = load_custom_agent(path)

            self.assertEqual(agent({}), [1, 2, 3])

    def test_load_custom_agent_rejects_file_without_agent_function(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "main.py"
            path.write_text("x = 1\n", encoding="utf-8")

            with self.assertRaisesRegex(SelectionError, "agent"):
                load_custom_agent(path)


if __name__ == "__main__":
    unittest.main()
