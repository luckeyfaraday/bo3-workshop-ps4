#!/usr/bin/env python3
"""Black Ops III Steam Workshop Zombies maps -> a jailbroken PS4 (GoldHEN), for offline play on your own copies.

Built on ItsJokerZz/PS4-BO3-Customs (its converter, patched by this project, and its in-game mod).

  One-time setup (see README.md):
  py bo3ps4.py doctor                   check the config, the PS4, the PC game, SteamCMD and the converter
  py bo3ps4.py deps                     upload the mod's runtime pack to /data/BO3-Customs on the PS4
  py bo3ps4.py loader                   make GoldHEN's plugin loader load the mod into BO3 (backs up GoldHEN's files)
  py bo3ps4.py pull-zones               copy the PS4 game's zones to the PC (start BO3 on the PS4 first)

  Maps:
  py bo3ps4.py port <workshop id> [...] download (SteamCMD) + convert + check + upload, one or many maps
  py bo3ps4.py port --file maps.txt     ... one Workshop ID per line
  py bo3ps4.py push-pending             upload maps that converted while the PS4 was unreachable
  py bo3ps4.py compat                   every map tried, and how it went
  py bo3ps4.py mark <id> ok|crash|broken [note]   record how a map played

  Pieces of `port`: convert <workshop id|map.ff|folder>, push <map>, status.

Writes on the PS4 go only to /data/BO3-Customs, plus GoldHEN's config.ini/plugins.ini in `loader` (backed up first).
"""
import argparse
import ftplib
import io
import json
import os
import posixpath
import re
import shutil
import struct
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP_ID = "311210"
REMOTE = "/data/BO3-Customs"
SPRX = f"{REMOTE}/BO3-Customs.sprx"
WRITE_ALLOWLIST = (f"{REMOTE}/",)
GH_CONFIG = "/data/GoldHEN/config.ini"
GH_PLUGINS = "/data/GoldHEN/plugins.ini"
# BO3 language zone prefixes. The PS4 game loads <lang>_<zone> for its system language, and a PS4 copy ships only some
# languages (an EU Spanish/Italian copy has es_ and it_, no en_), so every map and zm_levelcommon need those zones.
LANGS = {"en", "fr", "ge", "it", "es", "ru", "po", "ja", "tc", "sc", "ko", "bp", "ea", "ms", "ar"}
# PS4 zones that break conversions when used as shader sources (DOA's patch: its en_ asset points at data it lacks).
DONOR_EXCLUDE = ("cp_doa_bo3_patch.",)


# ---------------------------------------------------------------- config

class Config:
    def __init__(self, path: Path):
        if not path.exists():
            sys.exit(f"no {path.name}: copy config.example.json to {path.name} and fill it in (see README.md)")
        c = json.loads(path.read_text(encoding="utf-8"))
        self.ps4_ip = c["ps4_ip"]
        self.ftp_port = int(c.get("ftp_port", 2121))
        self.title_id = c.get("title_id", "auto")
        self.workdir = Path(c["workdir"])
        self.pc_game = Path(c["pc_game"]) if c.get("pc_game") else None
        self.steamcmd = Path(c["steamcmd"])
        self.steam_user = c.get("steam_user") or None
        self.ffport = Path(c.get("ffport") or HERE / "ffport" / "ffport.exe")
        self.upstream = Path(c.get("upstream_release") or HERE / "upstream")
        self.min_free_gb = float(c.get("min_free_gb", 25))
        self.zones = Path(c["ps4_zones"]) if c.get("ps4_zones") else self.workdir / "ps4-zones"

    out = property(lambda self: self.workdir / "out")
    work = property(lambda self: self.workdir / "porter-work")
    backups = property(lambda self: self.workdir / "backups")
    compat = property(lambda self: self.workdir / "compat.json")
    workshop = property(lambda self: self.steamcmd.parent / "steamapps" / "workshop" / "content" / APP_ID)


CFG: Config = None  # set in main()


def pc_game() -> Path:
    """The PC Black Ops III folder: from the config, else found through Steam's library list."""
    if CFG.pc_game:
        return CFG.pc_game
    for steam in (Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Steam",):
        vdf = steam / "steamapps" / "libraryfolders.vdf"
        if vdf.exists():
            for lib in re.findall(r'"path"\s+"([^"]+)"', vdf.read_text(encoding="utf-8", errors="replace")):
                game = Path(lib.replace("\\\\", "\\")) / "steamapps" / "common" / "Call of Duty Black Ops III"
                if (game / "zone" / "base.xpak").exists():
                    return game
    raise RuntimeError('PC Black Ops III not found: set "pc_game" in config.json to its folder')


# ---------------------------------------------------------------- PS4 (FTP)

def ftp() -> ftplib.FTP:
    f = ftplib.FTP()
    try:
        f.connect(CFG.ps4_ip, CFG.ftp_port, timeout=15)
    except OSError as e:
        raise RuntimeError(f"PS4 FTP at {CFG.ps4_ip}:{CFG.ftp_port} unreachable ({e}). Is GoldHEN running? "
                           "Is the PC on the same network, with any VPN off?") from e
    f.login()
    return f


def check_writable(remote: str) -> str:
    remote = posixpath.normpath(remote)
    if not any((remote + "/").startswith(p) for p in WRITE_ALLOWLIST):
        sys.exit(f"refusing to write {remote}: outside {WRITE_ALLOWLIST}")
    return remote


def ftp_makedirs(f: ftplib.FTP, remote_dir: str):
    check_writable(remote_dir + "/")
    parts = remote_dir.strip("/").split("/")
    for i in range(1, len(parts) + 1):
        try:
            f.mkd("/" + "/".join(parts[:i]))
        except ftplib.error_perm:
            pass


def ftp_read(f: ftplib.FTP, remote: str) -> bytes | None:
    buf = io.BytesIO()
    try:
        f.retrbinary(f"RETR {remote}", buf.write)
    except ftplib.error_perm:
        return None
    return buf.getvalue()


def remote_size(f: ftplib.FTP, remote: str) -> int | None:
    try:
        f.voidcmd("TYPE I")
        return f.size(remote)
    except ftplib.error_perm:
        return None


def upload_stream(f: ftplib.FTP, fh, size: int, remote: str):
    """Streams a file to the PS4 via <remote>.part + rename, so the game never sees a half-written file."""
    remote = check_writable(remote)
    ftp_makedirs(f, posixpath.dirname(remote))
    start, sent = time.time(), 0

    def progress(block):
        nonlocal sent
        sent += len(block)
        if size > 64 << 20:
            print(f"\r  {remote}: {sent / 2**20:.0f}/{size / 2**20:.0f} MB", end="", flush=True)

    f.storbinary(f"STOR {remote}.part", fh, blocksize=1 << 20, callback=progress)
    try:
        f.delete(remote)
    except ftplib.error_perm:
        pass
    f.rename(remote + ".part", remote)
    secs = max(time.time() - start, 0.001)
    print(f"\r  put {remote} ({size / 1e6:.1f} MB, {size / 1e6 / secs:.1f} MB/s)" + " " * 10)


def sfo_title(data: bytes) -> str:
    """TITLE from a param.sfo."""
    if data[:4] != b"\0PSF":
        return ""
    keys, values, count = struct.unpack_from("<III", data, 8)
    for i in range(count):
        key_off, fmt, length, _, value_off = struct.unpack_from("<HHIII", data, 20 + 16 * i)
        key = data[keys + key_off:data.index(b"\0", keys + key_off)]
        if key == b"TITLE":
            return data[values + value_off:values + value_off + length].rstrip(b"\0").decode(errors="replace")
    return ""


def title_id() -> str:
    """The console's Black Ops III title ID (region): from the config, else found among the installed apps."""
    if CFG.title_id and CFG.title_id != "auto":
        return CFG.title_id
    with ftp() as f:
        apps = [l.split()[-1] for l in _list(f, "/user/app") if l.split()[-1].startswith("CUSA")]
        for app in apps:
            if "Black Ops III" in sfo_title(ftp_read(f, f"/system_data/priv/appmeta/{app}/param.sfo") or b""):
                return app
    raise RuntimeError('Black Ops III not found on the PS4: set "title_id" in config.json')


def _list(f: ftplib.FTP, path: str) -> list[str]:
    lines = []
    try:
        f.retrlines(f"LIST {path}", lines.append)
    except ftplib.error_perm:
        pass
    return [l for l in lines if not l.endswith((" .", " .."))]


# ---------------------------------------------------------------- one-time setup

def runtime_pack() -> Path:
    zip_path = CFG.upstream / "Console.zip"
    if not zip_path.exists():
        sys.exit(f"{zip_path} not found: download Console.zip from the PS4-BO3-Customs v1.50 release (see README.md)")
    return zip_path


def cmd_deps(a):
    zf = zipfile.ZipFile(runtime_pack())
    with ftp() as f:
        for info in zf.infolist():
            if info.is_dir() or not info.filename.startswith("BO3-Customs/"):
                continue
            remote = "/data/" + info.filename
            if remote_size(f, remote) == info.file_size and not a.force:
                print(f"  same size, skipped {remote}")
                continue
            with zf.open(info) as fh:
                upload_stream(f, fh, info.file_size, remote)
        ftp_makedirs(f, f"{REMOTE}/usermaps")
    print("runtime pack uploaded; next: `bo3ps4.py loader`")


def backup(f: ftplib.FTP, remote: str) -> bytes | None:
    data = ftp_read(f, remote)
    if data is not None:
        CFG.backups.mkdir(parents=True, exist_ok=True)
        dest = CFG.backups / f"{Path(remote).name}.{time.strftime('%Y%m%d-%H%M%S')}"
        dest.write_bytes(data)
        print(f"  backed up {remote} -> {dest}")
    return data


def write_goldhen(f: ftplib.FTP, remote: str, data: bytes):
    """The only writes outside /data/BO3-Customs: GoldHEN's plugin settings, backed up first."""
    assert remote in (GH_CONFIG, GH_PLUGINS)
    f.storbinary(f"STOR {remote}", io.BytesIO(data))
    print(f"  wrote {remote}")


def section_lines(lines: list[str], name: str) -> list[str]:
    out, inside = [], False
    for line in lines:
        s = line.strip()
        if s.startswith("["):
            inside = s == f"[{name}]"
        elif inside:
            out.append(s)
    return out


def cmd_loader(a):
    """GoldHEN's plugin loader calls sceKernelLoadStartModule on every entry, so the mod's module_start runs inside
    BO3 with one plugins.ini line (it is not a GoldHEN plugin; the loader logs that it has no plugin_load, harmlessly)."""
    tid = title_id()
    with ftp() as f:
        cfg = backup(f, GH_CONFIG)
        if cfg is None:
            sys.exit(f"{GH_CONFIG} not found: is GoldHEN (2.3+) installed?")
        text = cfg.decode()
        if "PluginLoader_Enabled = 0" in text:
            write_goldhen(f, GH_CONFIG, text.replace("PluginLoader_Enabled = 0", "PluginLoader_Enabled = 1").encode())
        elif "PluginLoader_Enabled = 1" in text:
            print("  plugin loader already enabled")
        else:
            sys.exit("PluginLoader_Enabled not in GoldHEN's config.ini: enable plugins in Settings > GoldHEN > Plugins")
        old = backup(f, GH_PLUGINS)
        lines = (old or b"[settings]\nshow_load_notification=true\n").decode().replace("\r\n", "\n").rstrip("\n").split("\n")
        if any(line == SPRX for line in section_lines(lines, tid)):
            print(f"  [{tid}] already loads {SPRX}")
        else:
            if f"[{tid}]" in (line.strip() for line in lines):
                lines.insert(next(i for i, line in enumerate(lines) if line.strip() == f"[{tid}]") + 1, SPRX)
            else:
                lines += ["", f"[{tid}]", SPRX]
            write_goldhen(f, GH_PLUGINS, ("\n".join(lines) + "\n").encode())
    print("done. If GoldHEN's Plugins setting was off, turn it on in Settings > GoldHEN > Plugins (or reload GoldHEN).")


def cmd_pull_zones(a):
    """Copies the PS4 game's zones (read-only). Without Sony's shader compiler, the converter takes each map's
    shaders (technique sets) from these, and they tell which languages the console loads."""
    src = f"/mnt/sandbox/pfsmnt/{title_id()}-app0-patch0-union/zone"
    CFG.zones.mkdir(parents=True, exist_ok=True)
    with ftp() as f:
        lines = _list(f, src)
    if not lines:
        sys.exit("BO3's files aren't mounted: start Black Ops III on the PS4 (the main menu is enough) and retry")
    files = [(int(l.split()[4]), l.split()[-1]) for l in lines
             if l.split()[-1].endswith((".ff", ".fd")) and not l.split()[-1].startswith(DONOR_EXCLUDE)]
    total, done = sum(s for s, _ in files), 0
    f = ftp()
    for size, name in sorted(files):
        dest = CFG.zones / name
        part = dest.with_suffix(dest.suffix + ".part")
        for attempt in range(4):  # GoldHEN's FTP sometimes drops a long transfer (426): reconnect and retry
            if dest.exists() and dest.stat().st_size == size:
                break
            try:
                with open(part, "wb") as fh:
                    f.retrbinary(f"RETR {src}/{name}", fh.write, blocksize=1 << 20)
                part.replace(dest)
            except (ftplib.error_temp, ftplib.error_reply, OSError, EOFError) as e:
                print(f"\n  {name}: {e} (retry {attempt + 1})")
                try:
                    f.close()
                except Exception:
                    pass
                time.sleep(3)
                f = ftp()
        if not (dest.exists() and dest.stat().st_size == size):
            sys.exit(f"could not copy {name}")
        done += size
        print(f"\r  {done / 2**30:.1f}/{total / 2**30:.1f} GB  {name:40}", end="", flush=True)
    f.close()
    print(f"\n{len(files)} PS4 zones in {CFG.zones}; console languages: {', '.join(console_langs())}")


def cmd_doctor(a):
    ok = True

    def check(label: str, good: bool, hint: str = ""):
        nonlocal ok
        ok &= good
        print(f"  [{'ok' if good else '!!'}] {label}" + ("" if good or not hint else f"  ->  {hint}"))

    print(f"config: PS4 {CFG.ps4_ip}:{CFG.ftp_port}, workdir {CFG.workdir}")
    try:
        with ftp() as f:
            check("PS4 FTP reachable", True)
            gh = (ftp_read(f, GH_CONFIG) or b"").decode()
            check("GoldHEN plugin loader on", "PluginLoader_Enabled = 1" in gh, "bo3ps4.py loader")
            check("mod runtime pack on the PS4", remote_size(f, SPRX) is not None, "bo3ps4.py deps")
            plugins = (ftp_read(f, GH_PLUGINS) or b"").decode()
            check("plugins.ini loads the mod", SPRX in plugins, "bo3ps4.py loader")
        check(f"Black Ops III on the PS4: {title_id()}", True)
    except (RuntimeError, ftplib.Error) as e:
        check("PS4 reachable", False, str(e))
    try:
        check(f"PC Black Ops III: {pc_game()}", True)
    except RuntimeError as e:
        check("PC Black Ops III", False, str(e))
    check(f"patched converter: {CFG.ffport}", CFG.ffport.exists(), "scripts/build-ffport.ps1 (README.md)")
    check(f"runtime pack: {CFG.upstream / 'Console.zip'}", (CFG.upstream / "Console.zip").exists(), "README.md, step 2")
    check(f"SteamCMD: {CFG.steamcmd}", CFG.steamcmd.exists(), "download SteamCMD (README.md)")
    try:
        check(f"SteamCMD has a saved login ({'set' if CFG.steam_user else 'cached'})", bool(steam_user()))
    except RuntimeError as e:
        check("SteamCMD saved login", False, str(e))
    zones = list(CFG.zones.glob("*.ff")) if CFG.zones.exists() else []
    check(f"PS4 zones copied ({len(zones)})", len(zones) > 100, "start BO3 on the PS4, then bo3ps4.py pull-zones")
    CFG.workdir.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(CFG.workdir).free / 2**30
    check(f"free space in workdir: {free:.0f} GB", free >= CFG.min_free_gb, "big maps need 25+ GB while converting")
    print("all good" if ok else "fix the !! items above")


# ---------------------------------------------------------------- converting

def console_langs() -> list[str]:
    """The languages the PS4 copy ships, read from the zones pull-zones copied."""
    found = {p.name.split("_", 1)[0] for p in CFG.zones.glob("*_*.ff")} & LANGS
    if not found:
        sys.exit(f"no PS4 zones in {CFG.zones}: start BO3 on the PS4, then `bo3ps4.py pull-zones`")
    return sorted(found)


def workshop_dir(item: str) -> Path:
    return CFG.workshop / item


def map_zone(src: Path) -> Path | None:
    # The map zone is the biggest .ff; the rest are language zones (en_, bp_, ja_, ...).
    return src if src.suffix == ".ff" else max(src.rglob("*.ff"), key=lambda p: p.stat().st_size, default=None)


def convert_map(src: Path, languages: str, log_path: Path, name: str | None = None) -> tuple[int, Path]:
    """Runs the converter, printing each stage and keeping its full output in log_path."""
    ff = map_zone(src)
    if ff is None:
        raise RuntimeError(f"no map .ff under {src}")
    if not CFG.ffport.exists():
        raise RuntimeError(f"{CFG.ffport} not found: build the patched converter (scripts/build-ffport.ps1)")
    out = CFG.out / (name or ff.stem)
    cmd = [str(CFG.ffport), "t7", "convert", str(ff), "-o", str(out), "--force", "--progress", "--languages", languages,
           "--pc-reference", str(pc_game())]
    if any(CFG.zones.glob("*.ff")):
        cmd += ["--donors", str(CFG.zones)]
    else:
        print(f"warning: no PS4 zones in {CFG.zones} (bo3ps4.py pull-zones): materials with shaders will fail")
    CFG.work.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, FFPORTER_WORK=str(CFG.work))  # multi-GB caches: keep them in the workdir
    stage = None
    with open(log_path, "w", encoding="utf-8") as log:
        proc = subprocess.Popen(cmd, cwd=CFG.ffport.parent, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace")
        for line in proc.stdout:
            log.write(line)
            if line.startswith("FFPORTER_FIDELITY "):
                try:
                    snap = json.loads(line[18:])
                except json.JSONDecodeError:
                    continue
                if snap.get("stage") != stage:
                    stage = snap.get("stage")
                    print(f"  [{snap.get('elapsed_seconds', 0) // 60:>3.0f}m] {stage} {snap.get('detail', '')[:70]}", flush=True)
        rc = proc.wait()
    if rc == 0 and src.is_dir():
        # The mod titles the map from workshop.json and shows previewimage.png / loadingimage.png.
        for fname in ("workshop.json", "previewimage.png", "loadingimage.png"):
            found = next(src.rglob(fname), None)
            if found and not (out / fname).exists():
                shutil.copy2(found, out / fname)
    return rc, out


def summarize(log_path: Path) -> dict:
    """Score and the distinct blocking problems from a converter log."""
    text = log_path.read_text(encoding="utf-8", errors="replace")
    lines = [l for l in text.splitlines() if not l.startswith("FFPORTER_FIDELITY")]
    score = re.findall(r"port fidelity: (\d+)%", text)
    problems = []
    for l in lines:
        if re.search(r"cannot convert|is not in the output|invalid PC GSC|could not read|failed checks|not read by the loader", l):
            p = re.sub(r"^\[[^\]]+\] ", "", re.sub(r"0x[0-9a-f]+", "X", l.strip()))
            if p not in problems:
                problems.append(p)
    return {"converted": any(l.startswith("map converted:") for l in lines),
            "score": int(score[-1]) if score else None, "problems": problems[:12]}


def lang_zone_copy(data: bytes, stem: str, lang: str) -> bytes:
    """A PS4 en_<zone>.ff renamed to <lang>_<zone>: the zone name sits in the fastfile header (0x248 bytes)."""
    old, new = f"en_{stem}\0".encode(), f"{lang}_{stem}\0".encode()
    at = data.find(old, 0, 0x248)
    if at < 0:
        raise RuntimeError(f"en_{stem} not found in its fastfile header")
    return data[:at] + new + data[at + len(old):]


def fill_languages(out: Path, stem: str, langs: list[str]):
    """Gives the converted map every language zone and sound bank the console will ask for, copying the English ones
    when the map ships no translation (the en sound banks are stubs that name no language)."""
    for lang in langs:
        if lang == "en":
            continue
        en_ff = out / f"en_{stem}.ff"
        if not (out / f"{lang}_{stem}.ff").exists() and en_ff.exists():
            (out / f"{lang}_{stem}.ff").write_bytes(lang_zone_copy(en_ff.read_bytes(), stem, lang))
            if (out / f"en_{stem}.xpak").exists():
                shutil.copy2(out / f"en_{stem}.xpak", out / f"{lang}_{stem}.xpak")
            print(f"  made {lang}_{stem}.ff from the English zone")
        for ext in ("sabl", "sabs"):
            en_bank = out / "snd" / "en" / f"{stem}.en.{ext}"
            bank = out / "snd" / lang / f"{stem}.{lang}.{ext}"
            if en_bank.exists() and not bank.exists():
                bank.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(en_bank, bank)


def ensure_shared_languages(f: ftplib.FTP, langs: list[str]):
    """The runtime pack's zm_levelcommon only has English zones; add the console's languages."""
    zf = zipfile.ZipFile(runtime_pack())
    en = {n: zf.read(f"BO3-Customs/zone/{n}") for n in ("en_zm_levelcommon.ff", "en_zm_levelcommon.xpak",
                                                         "snd/en/zm_levelcommon.en.sabl", "snd/en/zm_levelcommon.en.sabs")}
    for lang in langs:
        if lang == "en":
            continue
        wanted = {f"{lang}_zm_levelcommon.ff": lang_zone_copy(en["en_zm_levelcommon.ff"], "zm_levelcommon", lang),
                  f"{lang}_zm_levelcommon.xpak": en["en_zm_levelcommon.xpak"],
                  f"snd/{lang}/zm_levelcommon.{lang}.sabl": en["snd/en/zm_levelcommon.en.sabl"],
                  f"snd/{lang}/zm_levelcommon.{lang}.sabs": en["snd/en/zm_levelcommon.en.sabs"]}
        for rel, data in wanted.items():
            remote = f"{REMOTE}/zone/{rel}"
            if remote_size(f, remote) != len(data):
                upload_stream(f, io.BytesIO(data), len(data), remote)


def cmd_convert(a):
    src = workshop_dir(a.source) if a.source.isdigit() else Path(a.source)
    ff = map_zone(src)
    if ff is None:
        sys.exit(f"no map .ff under {src}")
    try:
        rc, _ = convert_map(src, a.languages, CFG.out / f"{ff.stem}.log", a.name)
    except RuntimeError as e:
        sys.exit(str(e))
    print(json.dumps(summarize(CFG.out / f"{ff.stem}.log"), indent=1))
    sys.exit(rc)


def push_map(src: Path, force: bool = False):
    files = [p for p in src.rglob("*") if p.is_file()]
    if not files:
        raise RuntimeError(f"nothing to push in {src}")
    with ftp() as f:
        for p in files:
            remote = f"{REMOTE}/usermaps/{src.name}/" + p.relative_to(src).as_posix()
            if remote_size(f, remote) == p.stat().st_size and not force:
                print(f"  same size, skipped {remote}")
                continue
            with open(p, "rb") as fh:
                upload_stream(f, fh, p.stat().st_size, remote)


def cmd_push(a):
    try:
        push_map(CFG.out / a.map if not Path(a.map).is_dir() else Path(a.map), a.force)
    except RuntimeError as e:
        sys.exit(str(e))
    print("Maps are scanned when the mod loads: restart BO3 to see it.")


# ---------------------------------------------------------------- Steam

def workshop_details(ids: list[str]) -> dict[str, dict]:
    """Title, size and tags of Workshop items (public Steam API, no key)."""
    form = {"itemcount": str(len(ids))} | {f"publishedfileids[{i}]": x for i, x in enumerate(ids)}
    req = urllib.request.Request("https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/",
                                 data=urllib.parse.urlencode(form).encode())
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            items = json.load(r)["response"]["publishedfiledetails"]
    except (OSError, KeyError, ValueError) as e:
        print(f"  (Steam details unavailable: {e})")
        return {}
    return {d["publishedfileid"]: {"title": d.get("title", ""), "size": int(d.get("file_size", 0) or 0),
                                   "app": str(d.get("consumer_app_id", "")), "tags": [t["tag"] for t in d.get("tags", [])]}
            for d in items if d.get("result") == 1}


def steam_user() -> str:
    """The account SteamCMD logs in with (its saved login lets downloads run unattended)."""
    if CFG.steam_user:
        return CFG.steam_user
    cfg = CFG.steamcmd.parent / "config" / "config.vdf"
    text = cfg.read_text(encoding="utf-8", errors="replace") if cfg.exists() else ""
    m = re.search(r'"Accounts"\s*\{\s*"([^"]+)"', text)
    if not m:
        raise RuntimeError(f"no saved SteamCMD login: run once by hand:  {CFG.steamcmd} +login <account> +quit")
    return m.group(1)


def _vdf_parse(text: str) -> dict:
    """Steam KeyValues text (quoted keys/values, braces) -> nested dicts, order kept."""
    stack, key = [{}], None
    for quoted, brace in re.findall(r'"((?:[^"\\]|\\.)*)"|([{}])', text):
        if brace == "{":
            child = {}
            stack[-1][key] = child
            stack.append(child)
            key = None
        elif brace == "}":
            stack.pop()
        elif key is None:
            key = quoted
        else:
            stack[-1][key] = quoted
            key = None
    return stack[0]


def _vdf_dump(node: dict, depth: int = 0) -> str:
    tab, out = "\t" * depth, []
    for k, v in node.items():
        if isinstance(v, dict):
            out += [f'{tab}"{k}"', f"{tab}{{"] + ([_vdf_dump(v, depth + 1).rstrip("\n")] if v else []) + [f"{tab}}}"]
        else:
            out.append(f'{tab}"{k}"\t\t"{v}"')
    return "\n".join(out) + "\n"


def prune_workshop_record():
    """Forgets Workshop items whose files are gone (--cleanup deletes finished downloads). SteamCMD otherwise reuses
    shared chunks from them and fails: 'reading chunk ... (File Not Found) (Missing game files)'."""
    acf = CFG.steamcmd.parent / "steamapps" / "workshop" / f"appworkshop_{APP_ID}.acf"
    if not acf.exists():
        return
    data = _vdf_parse(acf.read_text(encoding="utf-8", errors="replace"))
    root = data.get("AppWorkshop", {})
    gone = [i for i in root.get("WorkshopItemsInstalled", {}) if not (CFG.workshop / i).is_dir()]
    if not gone:
        return
    for section in ("WorkshopItemsInstalled", "WorkshopItemDetails"):
        for i in gone:
            root.get(section, {}).pop(i, None)
    shutil.copy2(acf, acf.with_suffix(".acf.bak"))
    acf.write_text(_vdf_dump(data), encoding="utf-8")
    print(f"  SteamCMD record: forgot {len(gone)} deleted item(s)")


def download(item: str, log_path: Path) -> Path:
    """Downloads (or re-validates) a Workshop item with SteamCMD's saved login; never prompts."""
    cmd = [str(CFG.steamcmd), "+login", steam_user(), "+workshop_download_item", APP_ID, item, "validate", "+quit"]
    partial = CFG.steamcmd.parent / "steamapps" / "workshop" / "downloads" / APP_ID / item
    # SteamCMD abandons a Workshop download after ~5 minutes ("Timeout downloading item") even while data flows;
    # running it again resumes, so big maps take several rounds.
    for attempt in range(1, 31):
        prune_workshop_record()
        with open(log_path, "a", encoding="utf-8") as log:
            log.write(f"\n$ steamcmd +login *** +workshop_download_item {APP_ID} {item} validate +quit  (try {attempt})\n")
            proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, errors="replace",
                                  timeout=6 * 3600)
            log.write(proc.stdout + proc.stderr)
        text = proc.stdout + proc.stderr
        if "Cached credentials not found" in text or "password:" in text or "Invalid Password" in text:
            raise RuntimeError(f"SteamCMD needs a login: run once by hand:  {CFG.steamcmd} +login <account> +quit")
        if f"Success. Downloaded item {item}" in text:
            return workshop_dir(item)
        errors = re.findall(r"ERROR! ([^\r\n]*?)(?:Unloading|$)", text, re.M)
        if not any("Timeout" in e for e in errors):
            raise RuntimeError("SteamCMD download failed: " + ("; ".join(errors) or text.strip().splitlines()[-1]))
        got = sum(p.stat().st_size for p in partial.rglob("*") if p.is_file()) if partial.is_dir() else 0
        print(f"  SteamCMD timed out (try {attempt}), {got / 2**30:.1f} GB so far; resuming", flush=True)
    raise RuntimeError("SteamCMD kept timing out (30 tries)")


# ---------------------------------------------------------------- compatibility list

def load_compat() -> dict:
    return json.loads(CFG.compat.read_text(encoding="utf-8")) if CFG.compat.exists() else {}


def record(item: str, **fields):
    db = load_compat()
    db.setdefault(item, {}).update(fields, updated=time.strftime("%Y-%m-%d %H:%M"))
    CFG.compat.parent.mkdir(parents=True, exist_ok=True)
    CFG.compat.write_text(json.dumps(db, indent=1, ensure_ascii=False), encoding="utf-8")


def cmd_compat(a):
    db = load_compat()
    if not db:
        print("no maps tried yet")
        return
    print(f"{'id':>11}  {'map':22} {'stage':10} {'score':>5} {'in game':8} title / first problem")
    for item, r in sorted(db.items(), key=lambda kv: kv[1].get("updated", "")):
        why = (r.get("problems") or [r.get("error", "")])[0] if r.get("stage") != "pushed" else ""
        print(f"{item:>11}  {r.get('map', '?'):22} {r.get('stage', '?'):10} {str(r.get('score') or ''):>5} "
              f"{r.get('in_game', ''):8} {r.get('title', '')[:34]}" + (f" | {why[:90]}" if why else ""))


def cmd_mark(a):
    if a.id not in load_compat():
        sys.exit(f"{a.id} is not in {CFG.compat}")
    record(a.id, in_game=a.result, note=a.note or "")
    print(f"{a.id}: {a.result}")


# ---------------------------------------------------------------- port

def port_one(item: str, details: dict, langs: list[str], a) -> bool:
    title = details.get("title") or item
    size_gb = details.get("size", 0) / 2**30
    print(f"\n=== {item}  {title}" + (f"  ({size_gb:.1f} GB)" if size_gb else ""))
    if details and details.get("app") not in ("", APP_ID):
        record(item, title=title, stage="skipped", error="not a Black Ops III item")
        print("  not a Black Ops III item, skipped")
        return False
    record(item, title=title, stage="started", problems=[], error="")
    CFG.out.mkdir(parents=True, exist_ok=True)
    log_path = CFG.out / f"{item}.log"
    t0 = time.time()
    try:
        need = max(3 * size_gb, CFG.min_free_gb)
        free = shutil.disk_usage(CFG.out).free / 2**30
        if free < need:
            raise RuntimeError(f"only {free:.0f} GB free in {CFG.out}, want {need:.0f} GB")
        src = workshop_dir(item)
        if not a.no_download:
            print("  downloading with SteamCMD ...", flush=True)
            src = download(item, log_path)
        ff = map_zone(src) if src.is_dir() else None
        if ff is None:
            raise RuntimeError(f"no map .ff in {src} (a mod or weapon pack, not a map?)")
        stem = ff.stem
        record(item, map=stem, stage="downloaded", download_s=round(time.time() - t0))
        t1 = time.time()
        rc, out = convert_map(src, ",".join(["en"] + [l for l in langs if l != "en"]), CFG.out / f"{stem}.log")
        s = summarize(CFG.out / f"{stem}.log")
        record(item, map=stem, score=s["score"], problems=s["problems"], convert_s=round(time.time() - t1))
        if rc != 0 or not s["converted"]:
            record(item, stage="failed")
            print(f"  FAILED ({len(s['problems'])} distinct problems), e.g. {s['problems'][:1]}")
            # A failed conversion's output is partial and can be many GB; the download stays for a retry.
            shutil.rmtree(out, ignore_errors=True)
            shutil.rmtree(CFG.work / "t7" / stem, ignore_errors=True)
            return False
        fill_languages(out, stem, langs)
        size_out = sum(p.stat().st_size for p in out.rglob("*") if p.is_file())
        record(item, stage="converted", out_gb=round(size_out / 2**30, 2))
        if a.no_push:
            print(f"  converted ({s['score']}%), not pushed")
            return True
        t2 = time.time()
        try:
            with ftp() as f:
                ensure_shared_languages(f, langs)
            push_map(out)
        except (OSError, ftplib.Error, RuntimeError) as e:  # PS4 off or unreachable: keep the output for push-pending
            record(item, stage="converted", error=f"push failed: {e}")
            print(f"  converted ({s['score']}%), but the upload failed ({e}); `bo3ps4.py push-pending` retries it")
            return True
        record(item, stage="pushed", push_s=round(time.time() - t2), in_game="untested", error="")
        print(f"  on the PS4 ({s['score']}%, {size_out / 2**30:.1f} GB) in {(time.time() - t0) / 60:.0f} min total")
        if a.cleanup:  # it's on the PS4; failed maps keep their download for a retry
            shutil.rmtree(out, ignore_errors=True)
            shutil.rmtree(CFG.work / "t7" / stem, ignore_errors=True)
            if src.is_relative_to(CFG.workshop):
                shutil.rmtree(src, ignore_errors=True)
        return True
    except (RuntimeError, OSError, subprocess.TimeoutExpired, ftplib.Error) as e:
        record(item, stage="error", error=str(e))
        print(f"  ERROR: {e}")
        return False


def cmd_push_pending(a):
    langs = console_langs()
    pending = [(item, r) for item, r in load_compat().items()
               if r.get("stage") in ("converted", "error") and r.get("map") and (CFG.out / r["map"] / f"{r['map']}.ff").exists()]
    if not pending:
        print("nothing waiting to be uploaded")
        return
    try:
        with ftp() as f:
            ensure_shared_languages(f, langs)
    except RuntimeError as e:
        sys.exit(str(e))
    for item, r in pending:
        out = CFG.out / r["map"]
        print(f"=== {item}  {r.get('title', '')}")
        fill_languages(out, r["map"], langs)
        t = time.time()
        push_map(out)
        record(item, stage="pushed", push_s=round(time.time() - t), in_game=r.get("in_game") or "untested", error="")
        if a.cleanup:
            shutil.rmtree(out, ignore_errors=True)
            shutil.rmtree(CFG.work / "t7" / r["map"], ignore_errors=True)
            shutil.rmtree(workshop_dir(item), ignore_errors=True)
    print("Restart BO3 to see them under CUSTOM.")


def cmd_port(a):
    ids = list(a.ids)
    if a.file:
        ids += [l.split()[0] for l in Path(a.file).read_text().splitlines() if l.strip() and not l.startswith("#")]
    ids = [i for i in dict.fromkeys(ids) if i.isdigit()]
    if not ids:
        sys.exit("give Workshop IDs (or --file with one ID per line)")
    langs = console_langs()
    print(f"console languages: {', '.join(langs)}; {len(ids)} map(s)")
    details = workshop_details(ids)
    ok = [i for i in ids if port_one(i, details.get(i, {}), langs, a)]
    print(f"\n{len(ok)}/{len(ids)} maps {'converted' if a.no_push else 'on the PS4'}."
          + ("" if a.no_push else " Restart BO3 to see them under CUSTOM, then `bo3ps4.py mark <id> ok|crash`."))
    cmd_compat(a)


def cmd_status(_):
    try:
        with ftp() as f:
            for path in (REMOTE, f"{REMOTE}/zone", f"{REMOTE}/usermaps"):
                print(f"== {path}\n" + "\n".join(_list(f, path)))
            gh = (ftp_read(f, GH_CONFIG) or b"").decode()
            print("== GoldHEN plugin loader:", "on" if "PluginLoader_Enabled = 1" in gh else "OFF")
            print((ftp_read(f, GH_PLUGINS) or b"(no plugins.ini)").decode())
    except RuntimeError as e:
        sys.exit(str(e))


def main():
    global CFG
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default=str(HERE / "config.json"))
    sub = p.add_subparsers(required=True)
    sub.add_parser("doctor", help="check the setup").set_defaults(fn=cmd_doctor)
    s = sub.add_parser("deps", help="upload the mod's runtime pack"); s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_deps)
    sub.add_parser("loader", help="make GoldHEN load the mod into BO3").set_defaults(fn=cmd_loader)
    sub.add_parser("pull-zones", help="copy the PS4 game's zones (BO3 must be running)").set_defaults(fn=cmd_pull_zones)
    s = sub.add_parser("port", help="download + convert + upload Workshop maps")
    s.add_argument("ids", nargs="*"); s.add_argument("--file", help="text file, one Workshop ID per line")
    s.add_argument("--no-download", action="store_true", help="use the copy already downloaded")
    s.add_argument("--no-push", action="store_true", help="convert only")
    s.add_argument("--cleanup", action="store_true", help="after uploading, delete the converted copy and the download")
    s.set_defaults(fn=cmd_port)
    s = sub.add_parser("push-pending", help="upload maps that converted while the PS4 was unreachable")
    s.add_argument("--cleanup", action="store_true"); s.set_defaults(fn=cmd_push_pending)
    sub.add_parser("compat", help="every map tried").set_defaults(fn=cmd_compat)
    s = sub.add_parser("mark", help="record how a map played"); s.add_argument("id")
    s.add_argument("result", choices=["ok", "crash", "broken"]); s.add_argument("note", nargs="?")
    s.set_defaults(fn=cmd_mark)
    s = sub.add_parser("convert", help="convert one map (no upload)"); s.add_argument("source"); s.add_argument("--name")
    s.add_argument("--languages", default="en"); s.set_defaults(fn=cmd_convert)
    s = sub.add_parser("push", help="upload a converted map"); s.add_argument("map"); s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_push)
    sub.add_parser("status", help="what's on the PS4").set_defaults(fn=cmd_status)
    a = p.parse_args()
    CFG = Config(Path(a.config))
    a.fn(a)


if __name__ == "__main__":
    main()
