import tempfile
import unittest
from pathlib import Path

from pyro_agent import fds_tools


class GeometryMaterialMappingTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.config = {"case_root": str(self.root)}
        self.source_id = "multi_surface_source"
        (self.root / f"{self.source_id}.fds").write_text(
            "&HEAD CHID='multi_surface_source' /\n"
            "&TIME T_END=0.1 /\n"
            "&MESH IJK=10,10,10, XB=0,1,0,1,0,1 /\n"
            "&SURF ID='BASE', COLOR='GRAY' /\n"
            "&SURF ID='HOT', COLOR='RED' /\n"
            "&SURF ID='WALL', COLOR='BLUE' /\n"
            "&TAIL /\n",
            encoding="utf-8",
        )
        vertices = [
            (0.2, 0.2, 0.2), (0.8, 0.2, 0.2), (0.8, 0.8, 0.2), (0.2, 0.8, 0.2),
            (0.2, 0.2, 0.8), (0.8, 0.2, 0.8), (0.8, 0.8, 0.8), (0.2, 0.8, 0.8),
        ]
        faces = [
            (1, 3, 2), (1, 4, 3), (5, 6, 7), (5, 7, 8),
            (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6),
            (3, 4, 8), (3, 8, 7), (4, 1, 5), (4, 5, 8),
        ]
        lines = ["o two_surface_cube"]
        lines.extend(f"v {x} {y} {z}" for x, y, z in vertices)
        for index, (a, b, c) in enumerate(faces):
            if index == 0:
                lines.append("usemtl hot_face")
            elif index == 6:
                lines.append("usemtl wall_face")
            lines.append(f"f {a} {b} {c}")
        (self.root / "two_surface_cube.obj").write_text("\n".join(lines) + "\n", encoding="ascii")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_obj_material_groups_map_to_fds_face_surface_indexes(self):
        imported = fds_tools.add_geometry_mesh_to_case(
            self.config, self.source_id, "multi_surface_copy", "two_surface_cube.obj",
            "CAD_CUBE", "BASE", material_surface_map={
                "hot_face": "HOT", "wall_face": "WALL",
            },
        )
        case = fds_tools.inspect_case(self.config, imported["case_id"])
        geometry = next(item for item in case["records"] if item["record"] == "GEOM")

        self.assertEqual(geometry["fields"]["SURF_ID"], ["BASE", "HOT", "WALL"])
        face_surface_indexes = geometry["fields"]["FACES"][3::4]
        self.assertEqual(face_surface_indexes.count(2), 6)
        self.assertEqual(face_surface_indexes.count(3), 6)
        self.assertEqual(imported["material_surface_map"], {"hot_face": "HOT", "wall_face": "WALL"})

    def test_unmapped_obj_material_is_rejected_without_creating_case_copy(self):
        with self.assertRaisesRegex(fds_tools.ToolError, "没有映射"):
            fds_tools.add_geometry_mesh_to_case(
                self.config, self.source_id, "unmapped_copy", "two_surface_cube.obj",
                "CAD_CUBE", "BASE", material_surface_map={"hot_face": "HOT"},
            )
        self.assertFalse((self.root / "unmapped_copy.fds").exists())

    def test_boundary_comparison_summarizes_geometry_without_large_arrays(self):
        imported = fds_tools.add_geometry_mesh_to_case(
            self.config, self.source_id, "compare_copy", "two_surface_cube.obj",
            "CAD_CUBE", "BASE", material_surface_map={
                "hot_face": "HOT", "wall_face": "WALL",
            },
        )
        diff = fds_tools.compare_boundary_records(self.config, self.source_id, imported["case_id"])

        self.assertFalse(diff["equal"])
        self.assertEqual(diff["change_count"], 1)
        change = diff["changes"][0]
        self.assertEqual((change["change"], change["record_type"], change["id"]),
                         ("added", "GEOM", "CAD_CUBE"))
        summary = change["after"]["_mesh_summary"]
        self.assertEqual((summary["vertices"], summary["triangles"]), (8, 12))
        self.assertNotIn("VERTS", change["after"])
        self.assertNotIn("FACES", change["after"])


if __name__ == "__main__":
    unittest.main()
