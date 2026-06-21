# MCTS-PIMC ハイブリッドエージェント 設計メモ

> 目的：「強いデッキ（ハイブリッド）＋それを操縦できる探索エージェント」で mono-Crustle の天井(~66%)を超える。
> 関連：[[crustle_pdca]]（デッキ改良サイクル1〜3でデッキ路線は天井と判明）。
> 記録：2026-06-21

---

## 0. 背景（なぜ探索か）
ルールベースの変種C(ハイブリッド)は v1 −7pt / v2 −4pt で失敗。原因は**「ハイブリッドの操縦が手書きルールに複雑すぎた」**
（後出しでex判明、メガを晒さない判断、エネ配分…）。本物のゲームエンジンで数手先を読む**探索**なら、
“メガを晒すと負ける”を**探索が自分で発見**できる＝操縦問題を直接解決する。

## 1. MCTS vs RL の判定 → **MCTS(PIMC) を採用**
| | MCTS(PIMC) | RL(AlphaZero) | RL(進化・線形) |
|---|---|---|---|
| 現実性 | **◎** | △〜○ | ○ |
| 学習 | 不要（既存ヒューリスティックをrollout流用） | 要・GPU/Linux・self-play律速 | 要・128対局/世代 |
| 提出 | 軽量(torch不要, main.py+cg/) | torch同梱必須・推論重い | 超軽量(線形17重み) |
| 操縦の賢さ | 先読みで自動獲得 | 高いが学習依存 | 低い(先読み無=今と同質) |
- **理由**：学習不要・既存資産(変種Cのscore)を評価に流用・先読みで操縦を獲得＝我々の課題に最適。
- RL(AlphaZero=kiyotah)は完成形が存在し可能だが、過剰・重い・torch同梱・x86Linux/GPU前提。
- hmnshudhmn24のPPOは**看板倒れ**（環境未接続・88%グラフ捏造）＝参考外。
- 補完案：makimakiai型の**進化的線形重み調整**で評価関数をself-play最適化（安価・直交する強化）。

## 2. 環境：専用 Search API（最重要発見）
`data/sample_submission/cg/api.py`:
- `search_begin(obs, your_deck, your_prize, opponent_deck, opponent_prize, opponent_hand, opponent_active, manual_coin)` → 根 `SearchState(observation, searchId)`
  - 本戦の `battle_ptr` とは別の **`agent_ptr`** 上で動く（本戦を汚さない）。
  - 隠れ情報（相手手札/山/サイド、自山/サイド）は**予測値を渡す＝PIMC/決定化が前提**。枚数不一致は ValueError。
  - `obs.search_begin_input` が必須（agentに渡るobsそのものを渡す）。`manual_coin=True` でコインも固定可。
- `search_step(searchId, select)` → 次の `SearchState`。**親IDは破壊されず**、同じ親から別の手を試せば分岐。
- `search_release(searchId)` / `search_end()` でメモリ解放・再利用。

## 3. PoC（2026-06-21・本リスク潰し済 ✅）
`/tmp/poc_search.py`（コンテナ内 libcg で実行）結果：
```
our decision: turn=2 ctx=0 options=8
search_begin OK (rootId 0) → search_step OK (child 1)
同じrootから別の手 → child 2（分岐成立・親保持）
child からさらに前進 → id 3（数手先までOK）
search_release/end OK
REAL battle continues after search: True（本戦は無傷）
=> forward_sim=OK branch=True deeper=True real_unaffected=True
```
→ **前方シミュレーション・分岐・深掘り・破棄・本戦非干渉、すべて成立。MCTSの土台は確認済み。**

## 3.5 実証PoC2：最小MCTS が ヒューリスティック単体を上回るか（2026-06-21 ✅）
`submissions/crustle_mcts_v1/`（変種Cの評価関数を rollout 流用した flat Monte Carlo + PIMC）を実装し、
**同一デッキ(ハイブリッド)で「MCTS操縦」vs「ヒューリスティック操縦(変種C)」を head-to-head 計測**。

| 設定 | 結果(20戦, 同一デッキ) |
|---|---|
| 相手モデル=全イシズマイ(無害), depth30 | MCTS **0-4**（4戦）＝劣化。原因：相手が無害で rollout が甘い／浅く葉評価がサイド差で序盤≒0 |
| **相手モデル=mirror(MY_DECK), depth80, budget2.0s** | **MCTS 16勝 / HEUR 4勝 ＝ 80%**（二項検定 p≈0.6%＝有意） |

- **結論：最小MCTSは、相手モデルを競合化し rollout を深くすれば、greedy ヒューリスティックを明確に上回る。** 「探索による操縦＞greedy」を実証。
- **教訓（PIMCの肝）**：①相手の決定化(opponentデッキ予測)の質、②rolloutの深さ／葉評価、が探索の質を左右する。無害な相手・浅い探索は逆効果。
- 速度：~4.3s/試合(思考)、~0.06s/手（44手探索/試合、depth80×~5手 rollout）。エミュ環境でも実用域。
- 発動率：MCTS側50手中44手で探索発動（search_begin成功44/44）。残りは複数選択等でヒューリスティックに委譲。
- MCTSエージェント登録: `mcts_v1_1782026750956`（デッキ=変種Cハイブリッド）。

→ **次は「対フィールド勝率」で検証**（head-to-headは操縦差の証明。実戦力はフィールド計測が必要）。MCTS×全フィールド×多試合は重く、ここで EC2(ネイティブx86) が活きる。

## 3.6 実証PoC3：対フィールド一部（少sim）— ★相手モデルの限界が露呈（2026-06-21）
MCTS(ハイブリッド)を代表6体に各6戦（home/away）当てた結果：

| 相手 | MCTS勝率 | 参考(変種Cヒューリスティック) |
|---|---|---|
| Dragapult ex(ex) | **33%** | ~50-70%（MCTSが劣る） |
| Mega Lucario ex(ex) | 67% | — |
| Iono(非ex) | **0%** | 0%（構造的・不変） |
| anti-wall Mega Lucario | **33%** | 改善していた |
| Base mirror | 50% | — |
| Ensemble-PIMC MCTS(強) | **83%** | — |

- **重大な発見**：head-to-head の 80% は**相手モデルが mirror(=相手も変種C)で“相手を完全に当てていた”ための過大評価**だった。
  実フィールドは多様で、mirror決定化は**非Crustle相手(Dragapult等)を誤モデル化**→ MCTSが壁を正しく優先できず劣化。
- **= PIMCの肝＝相手の決定化(アーキタイプ予測)が、対フィールド性能の最大ボトルネック。** mirror固定は mirror に過剰適合。
- 明るい材料：強い Ensemble-PIMC MCTS に 83%。素性は良い。
- **結論：EC2スケール前に「相手モデルの改善（相手の公開カードからアーキタイプ推定／メタ多様な決定化アンサンブル）」が先。** ここを直さず大規模計測しても伸びない。少サンプル(6戦)でノイズは大きい点も留意。

## 3.7 決定化v2（相手モデル改善）＋ローカル検証の天井（2026-06-21）
改善：①相手デッキ＝**公開カード(場/進化元/エネ/トラッシュ)から推定**（mirror固定をやめる）②rollout中の相手＝**汎用アグロ方策**（最大打点で殴る等。こちらのCrustle評価で相手を動かす無力化を解消）。

| 相手 | v1(mirror) | v2(公開推定) |
|---|---|---|
| Dragapult ex | 33% | **50%↑** |
| Mega Lucario ex | 67% | 67% |
| Iono(非ex) | 0% | 0%（構造的） |
| anti-wall Mega Lucario | 33% | 17%↓ |
| Base mirror | 50% | 33%↓ |
| Ensemble-PIMC MCTS(強) | 83% | 83% |

- Dragapultは改善したが mirror/anti-wall は悪化＝**6戦/相手はノイズ帯(±17%)で判別不能**。
- **より本質的な限界：現状の最小MCTSは“ほぼ探索していない”**（1決定化×~1 rollout/手、depth80）。SOTAは10決定化×80sim≈**800sim/手**。エミュ環境が遅すぎて sim を増やせず、MCTSに正当な機会を与えられていない。
- **= ローカル(amd64エミュ)検証の天井**：①MCTSを正当なsim数で回す ②十分な試合数で信頼ある計測、の両方が**ネイティブx86(EC2)無しには不可**。
- 現時点の結論：**機構は健全・素性は良い(強MCTSに83%)が、対フィールドで勝てるかは“正当なsim数＋十分な試合数”で測らないと判定不能。ここからはEC2が必要**。

## 4. アーキテクチャ（MCTS-PIMC + ヒューリスティックrollout）
```
agent(obs):
  if obs.select is None: return MY_DECK            # デッキ提出
  try:
    決定化を K 回:
      隠れ情報をサンプリング（自山/サイド=自デッキの未公開分, 相手山/手/サイド=メタ予測, 伏せactive予測）
      root = search_begin(obs, ...)
      M 回シミュレーション(PUCT/UCT):
        search_step で葉まで前進 → 葉評価:
          終局なら ±1/0（state.result）
          非終局は「変種Cの評価関数」で盤面value（or 軽いrollout: ヒューリスティックでdepth数手）
      訪問数/価値を集計
    return アンサンブル最良手（最多訪問）
  except / 時間予算切れ:
    return 変種Cのヒューリスティック手   # 堅牢fallback（投了回避）
```
- **評価/ロールアウト方策＝変種Cのscore_option**（これまでの資産を活用）。
- **時間予算 anytime**：1手 ~1.5s 目安で打ち切り、残バンク時間が閾値を切ったら探索停止しヒューリスティックに退避（maximim Ensemble-PIMC 実績：10決定化×80sim でタイムアウト0）。
- **提出物**：pure Python + 同梱 `cg/`。**torch不要**。

## 5. 制約・リスク（正直に）
- `libcg.so` は **x86-64 Linux 専用**。ローカル検証は Docker(amd64エミュ=遅い)。MCTSは重く、計測は少sim or 速いLinux機が要る場面あり。
- 律速は `search_step`(ffi+JSONパース)。sim数と時間予算のチューニングが勝敗を分ける。
- `agent_ptr` はプロセスグローバル＝探索も**fork並列前提**（run_tournament は既に fork 並列）。
- 決定化の質（相手デッキ予測）が探索の質に直結。まずは雑な予測＋我々のヒューリスティック評価で十分か検証。

## 6. 進め方（フェーズ）
1. ✅ **疎通PoC**：Search API が本戦非干渉で前方シミュレーション可能か（完了）。
2. **ハイブリッド構築の確定**：メタ準拠60枚（Crustle＋メガガルーラex＋オーガポンいしずえのめんex＋マシマシラ＋tech）。
3. **MCTS-PIMC実装**：変種Cの評価をrollout流用＋堅牢ラッパ＋時間予算。最小simで正しさ検証。
4. **計測**：少simで対Base→sim数を上げて勝率の伸びを確認。決定化数/sim数/時間予算をチューニング。
5. （余力）評価関数を進化的self-playで微調整。

## 7. 参考
- 公式MCTS/RLサンプル：`data/reference/kaggle_notebooks/kiyotah__reinforcement-learning-and-mcts-sample-code.ipynb`
- コミュニティSOTA：`maximim__ptcg-ensemble-pimc-mcts-agent.ipynb`（10決定化×80sim＋F0 rollout depth24＋fallback）
- API：`data/sample_submission/cg/api.py`（search_begin/step/release/end）
