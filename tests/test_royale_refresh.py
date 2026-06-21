import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

# server.py は `from selection import ...` を使うため tools/webapp を import パスに通す。
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "webapp"))

# server は sim_env 経由でネイティブシミュレータ(libcg.so / Linux用)を読み込むが、
# refresh/ladder ロジックのテストには不要。ダミーの sim_env に差し替えて import を通す。
if "sim_env" not in sys.modules:
    _fake = types.ModuleType("sim_env")
    for _name in ("to_observation_class", "battle_start", "battle_select",
                  "battle_finish", "set_active_battle", "render_option",
                  "card_name", "card_meta", "card_detail", "option_card",
                  "translate_logs", "card_image_file", "CARD_IMAGES_DIR",
                  "OptionType", "AreaType", "SelectContext", "EnergyType"):
        setattr(_fake, _name, None)
    sys.modules["sim_env"] = _fake

from tools.webapp import server


class RoyaleRefreshTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.royale_dir = Path(self._tmp.name)
        self.ranking_file = self.royale_dir / "ranking.json"
        self._patches = [
            mock.patch.object(server, "ROYALE_DIR", self.royale_dir),
            mock.patch.object(server, "ROYALE_RANKING_FILE", self.ranking_file),
        ]
        for p in self._patches:
            p.start()
        self.client = server.app.test_client()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def _write_meta(self, pid):
        (self.royale_dir / "_refresh.meta").write_text(
            json.dumps({"job_id": "x", "pid": pid}), encoding="utf-8")

    def _write_progress(self, done, total):
        (self.royale_dir / "_refresh.progress").write_text(
            json.dumps({"done": done, "total": total}), encoding="utf-8")

    def _agents(self, n):
        return [{"id": f"a{i}", "name": f"AI{i}", "deck": []} for i in range(n)]

    # --- 指摘2: 二重起動拒否 ---
    def test_refresh_rejects_when_job_already_running(self):
        # 生存中のジョブ(meta に現プロセスPID)があれば 409 を返し、子プロセスを起動しない。
        self._write_meta(os.getpid())
        with mock.patch.object(server, "list_user_agents", return_value=self._agents(2)), \
                mock.patch.object(server.subprocess, "Popen") as popen:
            r = self.client.post("/api/royale/refresh", json={})
        self.assertEqual(r.status_code, 409)
        self.assertIn("error", r.get_json())
        popen.assert_not_called()

    def test_refresh_allows_when_prev_pid_lingers_but_completed(self):
        # 完了済み(done>=total)なら、PIDがゾンビとして生存判定されても409にせず起動する。
        self._write_meta(os.getpid())          # まだ生存判定されるPID
        self._write_progress(10, 10)           # ただし前回ジョブは完了済み
        with mock.patch.object(server, "list_user_agents", return_value=self._agents(2)), \
                mock.patch.object(server.subprocess, "Popen") as popen:
            popen.return_value.pid = 2_000_000_000
            r = self.client.post("/api/royale/refresh", json={})
        self.assertEqual(r.status_code, 200)
        self.assertIn("job_id", r.get_json())
        popen.assert_called_once()

    def test_refresh_resets_stale_progress_so_status_not_falsely_complete(self):
        # 新ジョブ開始時に前回の done==total を消し、即時GETで誤完了表示しない。
        self._write_progress(10, 10)           # 前回の完了進捗が残っている
        with mock.patch.object(server, "list_user_agents", return_value=self._agents(2)), \
                mock.patch.object(server.subprocess, "Popen") as popen:
            popen.return_value.pid = 2_000_000_000
            r = self.client.post("/api/royale/refresh", json={})
        self.assertEqual(r.status_code, 200)
        self.assertFalse((self.royale_dir / "_refresh.progress").exists())
        # 子プロセス(モック)が未起動でも、完了とは判定されないこと。
        j = self.client.get("/api/royale/refresh").get_json()
        self.assertFalse(j["complete"])

    # --- 指摘1/3: ステータス running / complete / failed ---
    def test_status_running_when_pid_alive_and_unfinished(self):
        self._write_meta(os.getpid())
        self._write_progress(1, 4)
        j = self.client.get("/api/royale/refresh").get_json()
        self.assertTrue(j["running"])
        self.assertFalse(j["complete"])

    def test_status_complete_overrides_lingering_pid(self):
        # 全試合消化済みなら、終了直後にPIDが生存判定されても complete を優先する。
        self._write_meta(os.getpid())
        self._write_progress(4, 4)
        j = self.client.get("/api/royale/refresh").get_json()
        self.assertFalse(j["running"])
        self.assertTrue(j["complete"])

    def test_status_failed_when_pid_dead_and_unfinished(self):
        # 異常終了: PIDは死んでいて、かつ未消化 → running/complete とも False。
        self._write_meta(2_000_000_000)
        self._write_progress(1, 4)
        j = self.client.get("/api/royale/refresh").get_json()
        self.assertFalse(j["running"])
        self.assertFalse(j["complete"])

    # --- 指摘4: 削除済みAIをランキングから除外 ---
    def test_ladder_excludes_deleted_agents(self):
        standings = [
            {"id": "a0", "name": "AI0", "winRate": 0.9, "wins": 9, "losses": 1, "draws": 0},
            {"id": "gone", "name": "削除済", "winRate": 0.8, "wins": 8, "losses": 2, "draws": 0},
            {"id": "a1", "name": "AI1", "winRate": 0.7, "wins": 7, "losses": 3, "draws": 0},
        ]
        self.ranking_file.write_text(
            json.dumps({"standings": standings}), encoding="utf-8")
        with mock.patch.object(server, "list_user_agents",
                               return_value=[{"id": "a0", "name": "AI0", "deck": []},
                                             {"id": "a1", "name": "AI1", "deck": []}]):
            ladder = server._royale_ladder()
        ids = [s["id"] for s in ladder]
        self.assertEqual(ids, ["a0", "a1"])
        self.assertEqual([s["rank"] for s in ladder], [1, 2])


if __name__ == "__main__":
    unittest.main()
