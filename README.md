# bo3-workshop-ps4

Play Black Ops III **Steam Workshop Zombies maps on a jailbroken PS4**, with one command per map:

```
py bo3ps4.py port 2112576356      # LEVIATHAN: download -> convert -> upload -> it appears under CUSTOM in BO3
```

It downloads the map with SteamCMD, converts it for the PS4, checks the result, uploads it over FTP, and keeps a list
of which maps work. Several of the most popular Workshop maps play on a real console with it (see
[COMPATIBILITY.md](COMPATIBILITY.md)).

**For your own copies, offline only.** You need the game on both PC (Steam) and PS4. Don't take a modded console
online, and don't share converted maps: they contain the map authors' work and Activision's assets. This repository
contains no game files.

## Built on PS4-BO3-Customs

The hard parts are [ItsJokerZz/PS4-BO3-Customs](https://github.com/ItsJokerZz/PS4-BO3-Customs) (MIT):

- **FFPorter**, the converter that turns PC fastfiles into PS4 ones, and
- **BO3-Customs.sprx**, the in-game mod that loads custom maps and adds the CUSTOM tab to Zombies map select.

This project adds:

- **Fixes to FFPorter** ([patches/](patches)), so conversions work without Sony's PS4 SDK shader compiler, using
  shaders from your own console's game files instead:
  - the command line accepts its own shipped PC image (v1.50's `convert` always failed);
  - technique sets inside texture combos and embedded in materials are taken from PS4 zones;
  - PS4 zones store shader data once and share it; the shared data is carried along, and copies that share data
    with models are avoided;
  - technique sets the retail PS4 game never shipped (DLC-only or mod-tools-made) use the nearest PS4 one that
    only **drops** features (a substitute that adds features samples textures the material lacks, and crashes);
  - a GSC opcode table fix (`WaitTill` takes a count byte) and a PS4 stand-in for the PC-only
    `clearallcharactertables`;
  - lower memory use while checking big maps.
- **The pipeline** ([bo3ps4.py](bo3ps4.py)): SteamCMD downloads (resuming its 5-minute timeouts), the console's
  language zones (an EU Spanish/Italian copy loads `es_`/`it_` zones and crashes without them), GoldHEN plugin
  loading, uploads, and a compatibility list.

## Requirements

- **PS4** on a GoldHEN-supported firmware, with **GoldHEN 2.3+** (plugin loader and FTP), and **Black Ops III
  v1.33**. Tested: EU `CUSA02626` on 7.55. Other regions should work (the mod checks the game's code before it
  hooks anything), but are untested.
- **Windows PC** with:
  - **Black Ops III on Steam** (the converter reads the PC game's files); ~120 GB.
  - **SteamCMD**, logged in once with the Steam account that owns BO3.
  - **Python 3.10+**, **git**, and the **.NET 10 SDK** (to build the converter).
  - **16 GB RAM** (big maps use ~8-10 GB while converting) and **~40 GB free** for the work folder.
- PC and PS4 on the **same network**. A VPN on the PC can route the PS4's address into the tunnel; turn it off.

## Setup

1. **Get this repo** and build the patched converter:
   ```
   git clone https://github.com/luckeyfaraday/bo3-workshop-ps4 && cd bo3-workshop-ps4
   powershell -ExecutionPolicy Bypass -File scripts\build-ffport.ps1
   ```
   It clones PS4-BO3-Customs v1.50, applies [patches/](patches), and builds `ffport\ffport.exe`.
2. **Download `Console.zip`** from the [PS4-BO3-Customs v1.50 release](https://github.com/ItsJokerZz/PS4-BO3-Customs/releases/tag/v1.50)
   into `upstream\` (the in-game mod, its UI scripts and the shared `zm_levelcommon` zone).
3. **Configure:** copy `config.example.json` to `config.json` and set `ps4_ip`, `workdir` (a folder with room) and
   `steamcmd`. `pc_game` and `title_id` are found automatically when left empty/`auto`.
4. **SteamCMD login**, once, by hand (it asks for your password and Steam Guard code, then remembers the login):
   ```
   D:\steamcmd\steamcmd.exe +login <your Steam account> +quit
   ```
5. **PS4 side** (GoldHEN running):
   ```
   py bo3ps4.py deps        # the mod's runtime pack -> /data/BO3-Customs
   py bo3ps4.py loader      # GoldHEN's plugins.ini loads the mod into BO3 (backs up GoldHEN's files first)
   ```
   Turn on **Settings > GoldHEN > Plugins** if it is off.
6. **Start Black Ops III on the PS4**, then copy its zones (read-only, ~8 GB; the converter takes shaders from them):
   ```
   py bo3ps4.py pull-zones
   ```
7. `py bo3ps4.py doctor` should say *all good*.

## Use

```
py bo3ps4.py port 1168113418 798643901            # one or more Workshop IDs
py bo3ps4.py port --file maps.txt --cleanup       # a list; --cleanup deletes each map's files once it's on the PS4
py bo3ps4.py compat                               # what converted, what failed and why
py bo3ps4.py mark 798643901 ok                    # after playing it
```

Then **close BO3 completely and start it again** (the mod scans for maps when it loads): Zombies > map select >
**CUSTOM**.

A small map converts in a few minutes; LEVIATHAN takes ~25 minutes including download and upload.

## When a map doesn't work

- **Fails to convert:** `compat` shows the first problem; the full log is in `workdir\out\<map>.log`.
- **Crashes in game:** turn on GoldHEN's `TTYRedirect` so the game's own errors reach the kernel log, record the log
  (klog on port 3232) while it crashes, and open an issue with the log and the Workshop ID. Crashes so far came from
  shader substitutes and from assets the PS4 game doesn't have (Zombies Chronicles content).
- **"PS4 unreachable":** GoldHEN not running, the PC on another network, or a VPN.

## How it works

The converter runs the real PC and PS4 zone loaders to rebuild every asset in the PS4 layout: textures retiled for
the PS4 GPU, sounds re-encoded to MP3, GSC scripts remapped to PS4 opcodes, streamed data repacked. Shaders can't be
recompiled without Sony's SDK, so each technique set is copied from your PS4's own game zones by name (or the nearest
feature-dropping match). The mod then loads the result from `/data/BO3-Customs/usermaps` when BO3 starts.

## Credits

- **ItsJokerZz**: PS4-BO3-Customs (FFPorter and the BO3-Customs mod).
- **flatz**: the ELF/fself tools the mod's build uses.
- **ate47**: atian-cod-tools, which FFPorter uses to check converted scripts.
- **The GoldHEN team**.
- **The Workshop map authors**, whose maps these are.

## License

MIT (see [LICENSE](LICENSE)); the patches modify MIT-licensed PS4-BO3-Customs.
