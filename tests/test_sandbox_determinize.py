"""サンドボックスの What-if 探索で使う決定化ヘルパ build_determinization の検証。

sim_env は配布シミュレータ cg（libcg.so, Linux x86-64 専用）を import するため、
cg が無い環境（macOS 開発機など）では自動でスキップする。Docker / Linux 上で実行される。
"""
import unittest
from collections import Counter
from types import SimpleNamespace as NS

try:  # cg が無ければモジュールごと import 失敗する
    from tools.sim_env import build_determinization, _seen_cards, _hidden_pool
    HAVE_CG = True
except Exception:  # ImportError / OSError（libcg.so ロード失敗）
    HAVE_CG = False


def _poke(cid, energy_cards=(), tools=(), pre=()):
    return NS(id=cid,
              energyCards=[NS(id=e) for e in energy_cards],
              tools=[NS(id=t) for t in tools],
              preEvolution=[NS(id=p) for p in pre])


def _player(hand=None, active=(), bench=(), discard=(), deck_count=0, prize=(), hand_count=None):
    hand_list = None if hand is None else [NS(id=c) for c in hand]
    return NS(hand=hand_list,
              active=list(active), bench=list(bench),
              discard=[NS(id=c) for c in discard],
              deckCount=deck_count, prize=[NS(id=c) for c in prize],
              handCount=hand_count if hand_count is not None else (len(hand) if hand else 0))


@unittest.skipUnless(HAVE_CG, "cg（libcg.so）が無いためスキップ（Docker/Linux で実行）")
class HiddenPoolTests(unittest.TestCase):
    def test_seen_counts_hand_field_and_discard(self):
        pl = _player(
            hand=[1, 1, 2],
            active=[_poke(10, energy_cards=[99])],
            bench=[_poke(11, tools=[98]), None],
            discard=[3, 3],
        )
        seen = _seen_cards(pl)
        self.assertEqual(seen[1], 2)   # 手札の同名2枚
        self.assertEqual(seen[2], 1)
        self.assertEqual(seen[10], 1)  # アクティブ本体
        self.assertEqual(seen[99], 1)  # ついているエネルギー
        self.assertEqual(seen[11], 1)  # ベンチ本体（None は無視）
        self.assertEqual(seen[98], 1)  # ついているどうぐ
        self.assertEqual(seen[3], 2)   # トラッシュ

    def test_hidden_pool_excludes_seen_and_has_right_size(self):
        deck = [1] * 4 + [2] * 4 + [3] * 52          # 60枚
        seen = Counter({1: 2, 2: 1})                 # 見えている札
        pool = _hidden_pool(deck, seen, need=40)
        self.assertEqual(len(pool), 40)
        c = Counter(pool)
        # 見えている枚数を差し引いた残数を超えて含まれない
        self.assertLessEqual(c[1], 4 - 2)
        self.assertLessEqual(c[2], 4 - 1)


@unittest.skipUnless(HAVE_CG, "cg（libcg.so）が無いためスキップ（Docker/Linux で実行）")
class BuildDeterminizationTests(unittest.TestCase):
    def _obs(self, your_index=0):
        me = _player(hand=[1, 1], active=[_poke(10)], deck_count=30, prize=[0, 0, 0])
        # 相手手札は自分視点では非公開（hand=None, handCount で枚数のみ）
        opp = _player(hand=None, active=[_poke(20)], deck_count=28, prize=[0, 0, 0], hand_count=5)
        players = [me, opp] if your_index == 0 else [opp, me]
        return NS(current=NS(players=players, yourIndex=your_index))

    def test_partition_sizes_match_state(self):
        my_deck = [1] * 4 + [10] * 4 + [2] * 52
        opp_deck = [20] * 4 + [3] * 56
        det = build_determinization(self._obs(0), 0, my_deck, opp_deck)
        self.assertEqual(len(det["your_deck"]), 30)
        self.assertEqual(len(det["your_prize"]), 3)
        self.assertEqual(len(det["opponent_deck"]), 28)
        self.assertEqual(len(det["opponent_prize"]), 3)
        self.assertEqual(len(det["opponent_hand"]), 5)

    def test_mirror_prior_when_opp_deck_unknown(self):
        my_deck = [1] * 4 + [10] * 4 + [2] * 52
        det = build_determinization(self._obs(0), 0, my_deck, opp_deck=None)
        self.assertEqual(len(det["opponent_hand"]), 5)
        self.assertEqual(len(det["opponent_deck"]), 28)


if __name__ == "__main__":
    unittest.main()
