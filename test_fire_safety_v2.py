# -*- coding: utf-8 -*-
from revit_mcp import fire_safety_logic
import math

def test_geometry():
    floor_dims = [[40000, 60000]]
    core_center = (0, 0)
    # Simulate a wide lift core (15m x 10m)
    lift_core_bounds = (-7500, -5000, 7500, 5000)
    typical_h = 4000
    preset_fs = {
        "staircase_spec": {"riser": 150, "tread": 300, "width_of_flight": 1500, "landing_width": 1500}
    }
    num_lifts = 8
    lobby_w = 3000

    print("--- Calculating Requirements ---")
    sets = fire_safety_logic.calculate_fire_safety_requirements(
        floor_dims, core_center, lift_core_bounds, typical_h, preset_fs, num_lifts, lobby_w
    )
    print(f"Number of sets: {len(sets)}")
    for i, s in enumerate(sets):
        print(f"Set {i+1}: {s['type']} at {s['pos']}")

    print("\n--- Generating Manifest ---")
    levels_data = [{"id": "L1", "elevation": 0}, {"id": "L2", "elevation": 4000}]
    manifest = fire_safety_logic.generate_fire_safety_manifest(
        sets, levels_data, preset_fs["staircase_spec"], typical_h, preset_fs, lift_core_bounds, num_lifts, lobby_w
    )

    walls = manifest["walls"]
    floors = manifest["floors"]
    bounds = manifest["core_bounds"]

    print(f"Total Walls: {len(walls)}")
    print(f"Total Floors: {len(floors)}")
    
    # Check Set 1 (Central South)
    set1_walls = [w for w in walls if "SafetySet_1" in w["id"]]
    lobby1_walls = [w for w in set1_walls if "_LB_" in w["id"]]
    print(f"\nSet 1 (Central South) Lobby Walls: {len(lobby1_walls)}")
    
    # Check for 4 walls per level for lobby
    if len(lobby1_walls) == 4:
        print("Set 1 Lobby has exactly 4 walls. [PASS]")
    else:
        print(f"Set 1 Lobby has {len(lobby1_walls)} walls. [FAIL]")

    # Check for rotation
    lb1 = [w for w in lobby1_walls if "SafetySet_1_LB_N" in w["id"]][0]
    lx = abs(lb1["end"][0] - lb1["start"][0])
    print(f"Set 1 Lobby Width: {lx}mm")
    
    # With rotation, lobby width should be based on sd_nat (~6m) instead of sw_nat (~3.5m)
    if lx > 5000:
        print("Set 1 is Rotated (Landscape). [PASS]")
    else:
        print("Set 1 is NOT Rotated (Portrait). [FAIL]")

    # Check bounding box
    b1 = bounds[0]
    print(f"Set 1 Bounding Box: {b1}")
    if b1[1] <= lift_core_bounds[1] and b1[3] <= lift_core_bounds[1] + 10:
         print("Set 1 is correctly aligned to core boundary. [PASS]")
    else:
         print(f"Set 1 alignment: {b1[1]} to {b1[3]}. Core ymin: {lift_core_bounds[1]}. [INFO]")

if __name__ == "__main__":
    test_geometry()
