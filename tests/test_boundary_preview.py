import json
import tempfile
import unittest
from pathlib import Path

from pyro_agent import fds_tools
from pyro_agent.mcp_server import TOOLS


class BoundaryPreviewTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.config = {"case_root": str(self.root)}
        self.source = self.root / "preview_source.fds"
        self.source.write_text(
            "&HEAD CHID='preview_source' /\n"
            "&TIME T_END=0.1 /\n"
            "&MESH IJK=10,10,10, XB=0,1,0,1,0,1 /\n"
            "&SURF ID='WALL', COLOR='GRAY' /\n"
            "&VENT ID='INLET', XB=0,0,0.2,0.8,0.2,0.8, SURF_ID='OPEN' /\n"
            "&TAIL /\n",
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_preview_then_apply_writes_only_isolated_copy(self):
        original_bytes = self.source.read_bytes()
        preview = fds_tools.preview_boundary_field_edits(
            self.config, "preview_source", "preview_copy", "SURF",
            {"TMP_FRONT": 25.0}, target_id="WALL",
        )

        self.assertEqual(preview["target"]["id"], "WALL")
        self.assertEqual(preview["changes"], [{
            "field": "TMP_FRONT", "action": "add", "before": None, "after": 25.0,
            "fds_text_preview": "25.0",
        }])
        self.assertEqual(self.source.read_bytes(), original_bytes)
        self.assertFalse((self.root / "preview_copy.fds").exists())

        applied = fds_tools.apply_boundary_field_preview(self.config, preview["preview_id"])
        self.assertTrue(applied["readback_verified"])
        self.assertEqual(applied["preview_source_sha256"], preview["source_sha256"])
        self.assertEqual(self.source.read_bytes(), original_bytes)
        copy = fds_tools.inspect_case(self.config, "preview_copy")
        surf = next(item for item in copy["records"] if item["record"] == "SURF")
        self.assertEqual(surf["fields"]["TMP_FRONT"], 25.0)
        with self.assertRaises(fds_tools.ToolError):
            fds_tools.apply_boundary_field_preview(self.config, preview["preview_id"])

    def test_preview_then_apply_can_target_anonymous_record_by_index(self):
        preview = fds_tools.preview_boundary_field_edits(
            self.config, "preview_source", "vent_copy", "VENT",
            {"TMP_FRONT": 42.0}, target_index=1,
        )
        self.assertEqual(preview["target"]["type_index"], 1)
        applied = fds_tools.apply_boundary_field_preview(self.config, preview["preview_id"])
        self.assertTrue(applied["readback_verified"])
        vent = next(item for item in fds_tools.inspect_case(self.config, "vent_copy")["records"]
                    if item["record"] == "VENT")
        self.assertEqual(vent["fields"]["TMP_FRONT"], 42.0)

    def test_mcp_exposes_preview_then_token_only_apply(self):
        tools = {item["name"]: item["inputSchema"] for item in TOOLS}
        self.assertIn("preview_boundary_field_edits", tools)
        self.assertEqual(tools["edit_boundary_fields"]["required"], ["preview_id"])
        self.assertNotIn("updates", tools["edit_boundary_fields"]["properties"])

    def test_apply_rejects_source_changed_after_preview(self):
        preview = fds_tools.preview_boundary_field_edits(
            self.config, "preview_source", "stale_copy", "SURF",
            {"TMP_FRONT": 30.0}, target_id="WALL",
        )
        self.source.write_text(self.source.read_text(encoding="utf-8") + "! changed\n", encoding="utf-8")

        with self.assertRaisesRegex(fds_tools.ToolError, "源算例发生变化"):
            fds_tools.apply_boundary_field_preview(self.config, preview["preview_id"])
        self.assertFalse((self.root / "stale_copy.fds").exists())

    def test_preview_rejects_invalid_vent_plane_before_token_creation(self):
        with self.assertRaisesRegex(fds_tools.ToolError, "恰好一个轴厚度为零"):
            fds_tools.preview_boundary_field_edits(
                self.config, "preview_source", "invalid_vent_copy", "VENT",
                {"XB": [0.2, 0.8, 0.2, 0.8, 0.2, 0.8]}, target_id="INLET",
            )
        preview_dir = self.root / ".pyro_agent_previews" / "boundary_fields"
        self.assertFalse(preview_dir.exists())

    def test_expired_preview_is_removed_and_cannot_be_applied(self):
        preview = fds_tools.preview_boundary_field_edits(
            self.config, "preview_source", "expired_copy", "SURF",
            {"TMP_FRONT": 30.0}, target_id="WALL",
        )
        manifest_path = (self.root / ".pyro_agent_previews" / "boundary_fields"
                         / f"{preview['preview_id']}.json")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["created_at"] = 0
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        with self.assertRaisesRegex(fds_tools.ToolError, "预览已过期"):
            fds_tools.apply_boundary_field_preview(self.config, preview["preview_id"])
        self.assertFalse(manifest_path.exists())
        self.assertFalse((self.root / "expired_copy.fds").exists())


if __name__ == "__main__":
    unittest.main()
