"""刮削：把下载完成的文件补齐元数据、内嵌封面与同名 .lrc。

元数据来源由 songmeta.merge_sources 统一编排（config.yaml 的 scraper.order）：
  按顺序问网易云 / QQ / 酷狗 / 酷我 / Apple Music / MusicBrainz，逐字段合并
  「谁先给就用谁」，每个字段记录命中平台（detail.hits），逐源过程记录在 detail.logs。
前四源是华语主力；Apple 字段最全但华语要靠宽松匹配，排其后补手；
MusicBrainz 中文基本没有，放最后兜底欧美日韩。
任何一步失败都不影响整体，只在返回值里如实记录。
"""
import base64
import os
import re

import requests
from mutagen import File as MFile
from mutagen.flac import FLAC, Picture
from mutagen.id3 import (APIC, ID3, ID3NoHeaderError, TALB, TCON, TDRC, TIT2,
                         TPE1, TRCK)

frame_types = {"TIT2": TIT2, "TPE1": TPE1, "TALB": TALB, "TDRC": TDRC}
from mutagen.id3 import TXXX
from mutagen.mp4 import MP4, MP4Cover
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis

from . import songmeta
from .core import log_scraper, merged_config

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) TuneSync/1.0"}
TIMEOUT = 15

# 字段中文名，刮削日志与进度提示统一用它，别再直接把 album/genre 甩给用户看
FIELD_LABEL = {"album": "专辑", "genre": "流派", "style": "风格",
               "year": "年份", "cover": "封面", "lrc": "歌词"}


def label_hits(hits):
    """把 {'album': 'wy', ...} 变成 ['专辑<wy>', ...]，没命中的字段不出现。"""
    return [f"{FIELD_LABEL[k]}<{hits[k]}>" for k in FIELD_LABEL if hits.get(k)]


def _cfg(section, key, default=None):
    return merged_config().get(section, {}).get(key, default)


def source_order():
    """数据源启用顺序（config.yaml 的 scraper.order）。

    兼容旧值：只填了 netease / platform_meta 的老配置按新默认顺序跑，
    因为这两个值已不再是独立数据源（旧实现依赖自建网易云代理，现已全部下线）。
    """
    raw = _cfg("scraper", "order", None)
    if not isinstance(raw, (list, tuple)):
        return list(songmeta.DEFAULT_ORDER)
    picked = [str(x).lower() for x in raw
              if str(x).lower() in songmeta.SOURCE_SEARCH]
    return picked or list(songmeta.DEFAULT_ORDER)


def _year_of(value):
    """从日期/时间戳里取 4 位年份，取不到返回 ''。"""
    m = re.search(r"(1[89]\d{2}|20\d{2})", str(value or ""))
    return m.group(1) if m else ""


def read_tags(path):
    """读取文件里已有的 专辑/流派/风格/年份/封面，返回 dict（读不到就空串）。

    手动刮削时用来判断「哪些歌还没有标签」，也用来在扫描时回填索引。
    """
    out = {"album": "", "genre": "", "style": "", "year": 0, "cover": ""}
    try:
        m = MFile(path, easy=True)
        if not (m and m.tags):
            return out
        def one(*keys):
            for k in keys:
                v = m.get(k)
                if v:
                    return str(v[0] if isinstance(v, (list, tuple)) else v).strip()
            return ""
        out["album"] = one("album")
        out["genre"] = one("genre")
        out["style"] = one("style")
        y = one("date", "year", "originaldate")
        out["year"] = int(_year_of(y)) if _year_of(y) else 0
        # EasyID3 会把图片帧过滤掉，判断封面必须再读一次完整标签
        if not out["cover"]:
            try:
                full = MFile(path)
                out["cover"] = "1" if full is not None and full.tags and any(
                    k.startswith(("APIC", "covr", "metadata_block_picture", "WM/Picture"))
                    for k in full.tags) else ""
            except Exception:
                pass
    except Exception:
        pass
    return out


def _write_tags(path, meta):
    """按容器格式写标签，返回是否成功。"""
    ext = os.path.splitext(path)[1].lower()
    title = meta.get("title", "")
    artist = meta.get("artist", "")
    album = meta.get("album", "")
    year = str(meta.get("year") or "")
    track = meta.get("track")

    if ext == ".mp3":
        try:
            tags = ID3(path)
        except ID3NoHeaderError:
            tags = ID3()
        simple = {"TIT2": title, "TPE1": artist, "TALB": album, "TDRC": year}
        for frame, value in simple.items():
            if value:
                tags[frame] = frame_types[frame](encoding=3, text=str(value))
        if meta.get("genre"):
            tags["TCON"] = TCON(encoding=3, text=str(meta["genre"]))
        if meta.get("style"):
            tags.delall("TXXX:STYLE")
            tags.add(TXXX(encoding=3, desc="STYLE", text=str(meta["style"])))
        if track:
            tags["TRCK"] = TRCK(encoding=3, text=str(track))
        if "cover_base64" in meta:
            tags.delall("APIC")
            tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="cover",
                          data=meta["cover_base64"]))
        tags.save(path, v2_version=3)
        return True

    if ext in (".flac", ".ogg", ".oga", ".opus"):
        if ext == ".flac":
            f = FLAC(path)
        elif ext == ".opus":
            f = OggOpus(path)          # opus 也是 Ogg 封装，但用 Vorbis 类打开会解析失败
        else:
            f = OggVorbis(path)
        if title:
            f["title"] = [title]
        if artist:
            f["artist"] = [artist]
        if album:
            f["album"] = [album]
        if year:
            f["date"] = [year]
        if meta.get("genre"):
            f["genre"] = [str(meta["genre"])]
        if meta.get("style"):
            f["style"] = [str(meta["style"])]
        if track:
            f["tracknumber"] = [str(track)]
        if "cover_base64" in meta:
            pic = Picture()
            pic.data = meta["cover_base64"]
            pic.type = 3
            pic.mime = "image/jpeg"
            if isinstance(f, FLAC):
                f.add_picture(pic)     # FLAC 有原生图片块；VorbisComment 键只收字符串
            else:
                # Ogg 惯例：整个 Picture 块 base64 后放 metadata_block_picture 注释
                f["metadata_block_picture"] = [base64.b64encode(pic.write()).decode("ascii")]
        f.save()
        return True

    if ext in (".m4a", ".mp4"):
        f = MP4(path)
        if title:
            f["\xa9nam"] = [title]
        if artist:
            f["\xa9ART"] = [artist]
        if album:
            f["\xa9alb"] = [album]
        if year:
            f["\xa9day"] = [year]
        if meta.get("genre"):
            f["\xa9gen"] = [str(meta["genre"])]
        if meta.get("style"):
            f["----:com.apple.iTunes:STYLE"] = [str(meta["style"]).encode("utf-8")]
        if track:
            f["trkn"] = [(int(track), 0)]
        if "cover_base64" in meta:
            f["covr"] = [MP4Cover(meta["cover_base64"], imageformat=MP4Cover.FORMAT_JPEG)]
        f.save()
        return True

    # wav/ape/wma 等：只写 easy 标签，写不了就算了
    m = MFile(path, easy=True)
    if m is None:
        return False
    if not m.tags:
        m.add_tags()
    for key, value in (("title", title), ("artist", artist), ("album", album),
                       ("date", year), ("genre", meta.get("genre", "")),
                       ("style", meta.get("style", ""))):
        if value:
            m[key] = str(value)
    m.save()
    return True


def scrape(path, meta, sid=None):
    """补齐标签 / 封面 / 歌词。任何一步失败都只记录，不抛出。

    在线元数据统一走 songmeta.merge_sources（多源候选 + 字段级合并 + 溯源）。
    返回 detail 里带：
      meta  —— 刮削后文件的真实标签，调用方用它回写索引
      hits  —— 每个字段实际来自哪个平台（album/genre/year/cover/lrc）
      logs  —— 逐源过程明细，手动刮削页的「运行详情」直接展示它
    """
    detail = {"tags": False, "cover": False, "lrc": False, "errors": [],
              "meta": {}, "hits": {}, "logs": [], "source": ""}
    meta = dict(meta or {})
    if not meta.get("title"):
        meta["title"] = os.path.splitext(os.path.basename(path))[0]

    # 下载链路已经把平台携带的 album/cover/lrc 放进 meta 了，先当权威值，
    # 在线源只补空缺字段，不会覆盖已有信息
    enriched = songmeta.merge_sources(
        meta.get("title", ""), meta.get("artist", ""),
        order=source_order(), want_lrc=bool(_cfg("scraper", "save_lrc", True)),
    )
    detail["hits"] = dict(enriched.get("hits") or {})
    detail["logs"] = list(enriched.get("logs") or [])
    detail["source"] = enriched.get("platform") or ""
    for k in ("album", "cover", "year", "genre", "style", "lrc"):
        if not meta.get(k) and enriched.get(k):
            meta[k] = enriched[k]
    detail["logs"].insert(0, f"数据源：{detail['source'] or '无匹配'}")

    cover_bytes = b""
    if _cfg("scraper", "embed_cover", True):
        url = meta.get("cover") or ""
        if url:
            try:
                resp = requests.get(url, headers=UA, timeout=20)
                resp.raise_for_status()
                cover_bytes = resp.content
            except Exception as e:
                detail["errors"].append(f"封面下载失败：{str(e)[:60]}")
    if cover_bytes:
        try:
            sidecar = os.path.join(os.path.dirname(path), "cover.jpg")
            with open(sidecar, "wb") as f:
                f.write(cover_bytes)
            detail["cover"] = True
        except Exception as e:
            detail["errors"].append(f"封面落盘失败：{str(e)[:60]}")

    # 没拿到图就别往标签里塞空值：mutagen 的 APIC 会因为 data=None 直接抛 TypeError
    if cover_bytes:
        meta["cover_base64"] = cover_bytes
    else:
        meta.pop("cover_base64", None)
    try:
        detail["tags"] = _write_tags(path, meta)
    except Exception as e:
        detail["errors"].append(f"标签写入失败：{str(e)[:60]}")
    log_scraper.debug("刮削 %s：%s", os.path.basename(path), detail)

    if _cfg("scraper", "save_lrc", True) and meta.get("lrc"):
        try:
            with open(os.path.splitext(path)[0] + ".lrc", "w", encoding="utf-8") as f:
                f.write(meta["lrc"])
            detail["lrc"] = True
        except Exception as e:
            detail["errors"].append(f"歌词写入失败：{str(e)[:60]}")

    # 以文件里的真实标签为准回传，调用方据此更新索引
    final = read_tags(path)
    detail["meta"] = {
        "album": final["album"] or meta.get("album", ""),
        "genre": final["genre"] or meta.get("genre", ""),
        "style": final["style"] or meta.get("style", ""),
        "year": final["year"] or (_year_of(meta.get("year")) and int(_year_of(meta.get("year")))),
        "cover": "1" if (cover_bytes or final["cover"]) else "",
        "lrc": "1" if detail["lrc"] else "",
    }
    got = label_hits(detail["hits"]) or [k for k in ("album", "genre", "year", "cover")
                                         if detail["meta"].get(k)]
    detail["logs"].append("已写入：" + ("、".join(got) if got else "无字段写入"))
    return detail
