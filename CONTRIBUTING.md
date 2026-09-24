# Contributing

The most useful thing right now is **testing maps**. Port one, play it, and open a *Map report* issue, or add a
row to [COMPATIBILITY.md](COMPATIBILITY.md) in a PR.

Fixing a map usually means fixing the converter. Converter changes live in `patches/ffporter-fixes.diff`, against
PS4-BO3-Customs v1.50:

1. `scripts\build-ffport.ps1` clones and patches the source into `upstream\src`.
2. Edit it there, rebuild (`dotnet publish` as in the script), and test on the map.
3. Regenerate the diff from `upstream\src` (`git -C upstream\src diff -- Tool/src/*.cs > patches\ffporter-fixes.diff`)
   and include the map that made you do it in the PR.

Changes to the `t7_gsc` data tables go in `patches/apply-data-fixes.py`.

Ground rules: never commit game files (fastfiles, xpaks, sound banks, executables or dumps of them), converted maps,
or logs with account names in them.
