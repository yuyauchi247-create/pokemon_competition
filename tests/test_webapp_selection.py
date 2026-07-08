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
    touch_user_deck_opened,
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

    def test_list_user_decks_orders_by_updated_at_desc(self):
        """一覧は最終編集日時(updated_at)の新しい順。ファイルmtime順ではない
        （S3同期/rsyncでmtimeが書き換わっても順序が壊れないこと）。"""
        deck = [151] * 4 + [152] * 4 + [666] * 4 + [2] * 48
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)

            def mk(deck_id, name, updated_at=None):
                (base / f"{deck_id}.csv").write_text("\n".join(str(c) for c in deck) + "\n")
                meta = {"id": deck_id, "name": name, "visibility": "public"}
                if updated_at is not None:
                    meta["updated_at"] = updated_at
                (base / f"{deck_id}.json").write_text(json.dumps(meta, ensure_ascii=False))

            mk("old_1700000000000", "古い", updated_at=1700000000000)
            mk("new_1720000000000", "新しい", updated_at=1720000000000)
            mk("mid_1710000000000", "中間_updated_at無し")  # ID末尾の作成時刻(1710...)で代替

            order = [d["name"] for d in list_user_decks(decks_dir=base)]
            self.assertEqual(order, ["新しい", "中間_updated_at無し", "古い"])

    def test_save_and_update_record_updated_at(self):
        """save/update で updated_at(ms) が meta に記録される。"""
        deck = [151] * 4 + [152] * 4 + [666] * 4 + [2] * 48
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            saved = save_user_deck("t", deck, decks_dir=base)
            meta = json.loads((base / f"{saved['id']}.json").read_text())
            self.assertIsInstance(meta.get("updated_at"), int)
            self.assertGreater(meta["updated_at"], 0)

    def test_list_user_decks_orders_by_last_opened(self):
        """直近で「開いた」(opened_at) デッキが、編集が古くても先頭に来る。"""
        deck = [151] * 4 + [152] * 4 + [666] * 4 + [2] * 48
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)

            def mk(deck_id, name, updated_at, opened_at=None):
                (base / f"{deck_id}.csv").write_text("\n".join(str(c) for c in deck) + "\n")
                meta = {"id": deck_id, "name": name, "visibility": "public",
                        "updated_at": updated_at}
                if opened_at is not None:
                    meta["opened_at"] = opened_at
                (base / f"{deck_id}.json").write_text(json.dumps(meta, ensure_ascii=False))

            # 編集は古いが直近で開いたデッキが先頭。開いた後に編集したデッキはその編集時刻で並ぶ。
            mk("a_1700000000000", "編集古い_直近で開いた", updated_at=1700000000000,
               opened_at=1730000000000)
            mk("b_1720000000000", "編集新しい_開いていない", updated_at=1720000000000)
            mk("c_1725000000000", "開いた後に編集", updated_at=1725000000000,
               opened_at=1710000000000)

            order = [d["name"] for d in list_user_decks(decks_dir=base)]
            self.assertEqual(order, ["編集古い_直近で開いた", "開いた後に編集", "編集新しい_開いていない"])

    def test_touch_user_deck_opened_sets_opened_at_and_keeps_updated_at(self):
        """touch は opened_at のみ更新し、updated_at（最終編集時刻）は変えない。"""
        deck = [151] * 4 + [152] * 4 + [666] * 4 + [2] * 48
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            saved = save_user_deck("t", deck, decks_dir=base)
            before = json.loads((base / f"{saved['id']}.json").read_text())
            touch_user_deck_opened(saved["id"], decks_dir=base)
            after = json.loads((base / f"{saved['id']}.json").read_text())
            self.assertIsInstance(after.get("opened_at"), int)
            self.assertGreater(after["opened_at"], 0)
            self.assertEqual(after.get("updated_at"), before.get("updated_at"))
            self.assertEqual(after.get("name"), before.get("name"))


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
