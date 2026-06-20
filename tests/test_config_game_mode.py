import os
import tempfile
import unittest
from unittest.mock import patch

from app.config import config_reader as cr


class GameModeWorldgenTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cluster_dir = os.path.join(self.tmp.name, "cluster")
        os.makedirs(os.path.join(self.cluster_dir, "Master"), exist_ok=True)
        os.makedirs(os.path.join(self.cluster_dir, "Caves"), exist_ok=True)
        self.patcher = patch.object(cr, "CLUSTER_DIR", self.cluster_dir)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.tmp.cleanup()

    def test_game_mode_maps_to_endless_preset(self):
        self.assertEqual(cr._game_mode_preset("endless"), "ENDLESS_TOGETHER")
        self.assertEqual(cr._game_mode_preset("survival"), "SURVIVAL_TOGETHER")

    def test_write_cluster_ini_syncs_worldgen_even_if_unchanged(self):
        cr.write_cluster_ini({
            **cr._default_cluster_ini(),
            "GAMEPLAY.game_mode": "endless",
        })
        master_path = os.path.join(self.cluster_dir, "Master", "worldgenoverride.lua")
        with open(master_path, "w", encoding="utf-8") as handle:
            handle.write('return { preset_type = "SURVIVAL_TOGETHER" }')

        result = cr.write_cluster_ini({
            **cr.read_cluster_ini(),
            "GAMEPLAY.game_mode": "endless",
        })

        self.assertTrue(result["success"])
        self.assertTrue(result["worldgen_synced"])
        with open(master_path, "r", encoding="utf-8") as handle:
            content = handle.read()
        self.assertEqual(cr._parse_worldgen_preset(content), "ENDLESS_TOGETHER")

    def test_apply_preset_uses_explicit_game_mode(self):
        result = cr.apply_online_preset(game_mode="endless")
        self.assertTrue(result["success"])
        cluster = cr.read_cluster_ini()
        self.assertEqual(cluster["GAMEPLAY.game_mode"], "endless")
        master_path = os.path.join(self.cluster_dir, "Master", "worldgenoverride.lua")
        with open(master_path, "r", encoding="utf-8") as handle:
            content = handle.read()
        self.assertEqual(cr._parse_worldgen_preset(content), "ENDLESS_TOGETHER")

    def test_read_cluster_game_mode_without_file(self):
        self.assertEqual(cr.read_cluster_game_mode("survival"), "survival")
        self.assertFalse(cr.cluster_ini_exists())


if __name__ == "__main__":
    unittest.main()
