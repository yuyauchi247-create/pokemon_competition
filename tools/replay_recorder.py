"""対戦を Kaggle の replay.json 互換構造で記録する。

ローカルアプリ(server.py)・評価(run_tournament.py)の双方から使う。
各 battle_select の直前に record(obs, you, picks) を呼び、終了時 finalize(result) で
replay dict を得る。

互換性の注意:
- observation 本体(current/logs/select)は Kaggle と同一(同じ cg シミュレータのため)。
- Kaggle 実機の step/remainingOverageTime/steps 構造に合わせるが、kaggle-environments
  ラッパは同梱されないため、specification 等のenvメタは妥当なスタブ。可視化専用の
  visualize フィールドは付与しない。
- 非能動エージェントの observation は step キーを持たない(Kaggle 実機と同じ)。
"""


def _inactive_observation():
    # Kaggle の非能動エージェント observation は step を持たない。
    return {"select": None, "logs": [], "current": None,
            "search_begin_input": None, "remainingOverageTime": None}


class ReplayRecorder:
    def __init__(self, player_names=None, seed=None, episode_id=None):
        self.steps = []
        self.player_names = list(player_names) if player_names else ["Player1", "Player2"]
        self.seed = seed
        self.episode_id = episode_id
        self._final = None  # finalize 済み replay dict

    def record(self, obs, you, picks):
        """1ステップ記録する。obs=能動側の生observation(dict), you=能動side(0/1), picks=action。"""
        if self._final is not None:
            return
        you = 0 if you not in (0, 1) else you
        active_obs = dict(obs or {})
        active_obs.setdefault("search_begin_input", None)
        active_obs["step"] = len(self.steps)
        active_obs["remainingOverageTime"] = None
        active = {"action": list(picks) if picks is not None else [],
                  "info": {}, "observation": active_obs, "reward": 0, "status": "ACTIVE"}
        inactive = {"action": [], "info": {}, "observation": _inactive_observation(),
                    "reward": 0, "status": "INACTIVE"}
        step = [active, inactive] if you == 0 else [inactive, active]
        self.steps.append(step)

    def finalize(self, result):
        """result: 勝者index(0/1) or 引き分け(-1)。replay.json 互換 dict を返す。"""
        if self._final is not None:
            return self._final
        if result == 0:
            rewards = [1, -1]
        elif result == 1:
            rewards = [-1, 1]
        else:
            rewards = [0, 0]
        # 終了ステップ(両者 DONE)。観測は直前の能動側盤面を流用。
        last_current = None
        for st in reversed(self.steps):
            for a in st:
                if a["status"] == "ACTIVE" and a["observation"].get("current") is not None:
                    last_current = a["observation"]["current"]
                    break
            if last_current is not None:
                break
        done_step = []
        for i in range(2):
            obs = {"select": None, "logs": [], "current": last_current,
                   "search_begin_input": None, "remainingOverageTime": None}
            done_step.append({"action": [], "info": {}, "observation": obs,
                              "reward": rewards[i], "status": "DONE"})
        self.steps.append(done_step)
        self._final = {
            "name": "ptcg",
            "title": "PTCG Battle",
            "description": "",
            "version": "1.0.0",
            "schema_version": 1,
            "module_version": "local",
            "id": self.episode_id or "",
            "configuration": {"actTimeout": 0, "episodeSteps": 10000,
                              "runTimeout": 2000, "seed": self.seed},
            "info": {"TeamNames": self.player_names, "Agents": self.player_names,
                     "EpisodeId": self.episode_id, "LiveVideoPath": None},
            "specification": {},
            "rewards": rewards,
            "statuses": ["DONE", "DONE"],
            "steps": self.steps,
        }
        return self._final

    @property
    def replay(self):
        return self._final
