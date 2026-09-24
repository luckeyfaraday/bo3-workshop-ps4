"""Data-table fixes for FFPorter's t7_gsc tables (apply to Data/Shipped/t7_gsc in the source tree, or to the
extracted copy in <work>/data/t7_gsc). Found porting LEVIATHAN, 2026-09-24.

  py -3 apply-data-fixes.py <t7_gsc folder>
"""
import json
import sys
from pathlib import Path

folder = Path(sys.argv[1])

# 1. The op the table calls "WaitTill" (PC handler 0x7ff615b44510) is waittillmatch with a u8 count (acts:
#    WaitTillMatch2). Read with no operands, the count byte and the linker's padding byte decode as an opcode; that
#    only goes unnoticed when the padding happens to be 0 (reads as a Nop). LEVIATHAN's _zm_blockers.gsc has
#    padding 0x68 -> "invalid PC GSC opcode 0x6801". Checked against acts across 155,264 LEVIATHAN instructions.
ops_path = folder / "gsc_opcodes.json"
ops = json.loads(ops_path.read_text(encoding="utf-8"))
next(o for o in ops["ops"] if o["name"] == "WaitTill")["operands"] = "u8"
ops_path.write_text(json.dumps(ops, indent=1), encoding="utf-8")

# 2. clearallcharactertables is PC-only; rewrite its calls to gettime() (0 args, no side effects, result discarded).
builtins_path = folder / "ps4_builtins.json"
builtins = json.loads(builtins_path.read_text(encoding="utf-8"))
builtins["pc_only"]["4c0dc03d"].update(
    ps4="gettime",
    why="PC resets the zombie character tables before a map registers its own; the PS4 build has no such builtin, "
        "so the call becomes gettime() (0 arguments, no side effects, result discarded) and the stock tables stay")
builtins_path.write_text(json.dumps(builtins, indent=1), encoding="utf-8")
print("applied to", folder)
