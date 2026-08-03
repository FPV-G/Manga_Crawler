# -*- coding: utf-8 -*-
"""多源漫画适配器：拷贝漫画 / 再漫画 / 包子漫画 / 蛙漫3 / 如漫画 / 漫画柜。"""

from __future__ import annotations

import base64
import hashlib
import json
import random
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup
from Crypto.Cipher import AES


UA_CHROME = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class SourceError(Exception):
    pass


@dataclass
class SourceResult:
    id: str
    title: str
    cover: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ChapterItem:
    id: str
    name: str
    order: int
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ComicInfo:
    id: str
    title: str
    cover: str = ""
    chapters: list[ChapterItem] = field(default_factory=list)


class SourceAdapter:
    name = "base"
    label = "基础源"

    def search(self, keyword: str) -> list[SourceResult]:
        raise NotImplementedError

    def get_comic(self, result: SourceResult) -> ComicInfo:
        raise NotImplementedError

    def get_chapter_images(self, comic: ComicInfo, chapter: ChapterItem) -> list[str]:
        raise NotImplementedError

    def image_headers(self, url: str) -> dict[str, str]:
        return {}

    def decrypt_image(self, data: bytes) -> bytes:
        return data

    def force_ext(self) -> str | None:
        return None


# ---------------------------------------------------------------- 通用请求


def _http_get(
    session: requests.Session,
    url: str,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    timeout: float = 30.0,
    retries: int = 3,
) -> requests.Response:
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            resp = session.get(url, headers=headers, params=params, timeout=timeout)
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:
            last = exc
            if attempt < retries:
                time.sleep(min(2 ** attempt, 6))
    raise SourceError(f"GET failed: {url} ({last})")


def _http_post(
    session: requests.Session,
    url: str,
    headers: dict[str, str] | None = None,
    data: dict[str, Any] | None = None,
    timeout: float = 30.0,
    retries: int = 3,
) -> requests.Response:
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            resp = session.post(url, headers=headers, data=data, timeout=timeout)
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:
            last = exc
            if attempt < retries:
                time.sleep(min(2 ** attempt, 6))
    raise SourceError(f"POST failed: {url} ({last})")


# ---------------------------------------------------------------- Packer / LZString


_BASE36 = "0123456789abcdefghijklmnopqrstuvwxyz"


def _int_to_base36(num: int) -> str:
    if num == 0:
        return "0"
    out = ""
    while num > 0:
        out = _BASE36[num % 36] + out
        num //= 36
    return out


def unpack_dean_edwards(p: str, a: int, c: int, k: str) -> str:
    """还原 Dean Edwards Packer 混淆代码（如漫画用）。"""
    words = k.split("|")

    def encode(num: int) -> str:
        remainder = num % a
        prefix = "" if num < a else encode(num // a)
        suffix = chr(remainder + 29) if remainder > 35 else _int_to_base36(remainder)
        return prefix + suffix

    result = p
    for i in range(c - 1, -1, -1):
        value = words[i] if i < len(words) else ""
        if not value:
            continue
        token = encode(i)
        result = re.sub(rf"\b{re.escape(token)}\b", lambda _m: value, result)
    return result


def extract_packed_script(html: str) -> str:
    """从 HTML 提取 eval(function(p,a,c,k,e,d)...) 调用文本。"""
    start = html.find("eval(function(p,a,c,k,e,d)")
    if start == -1:
        start = html.find("eval(function (p,a,c,k,e,d)")
    if start == -1:
        raise SourceError("未找到 packed script")
    i = start + 4
    paren = 0
    in_string = False
    string_char = ""
    while i < len(html):
        ch = html[i]
        if in_string:
            if ch == "\\":
                i += 2
                continue
            if ch == string_char:
                in_string = False
            i += 1
            continue
        if ch in ('"', "'", "`"):
            in_string = True
            string_char = ch
        elif ch == "(":
            paren += 1
        elif ch == ")":
            paren -= 1
            if paren == 0:
                return html[start + 4 : i + 1].strip()
        i += 1
    raise SourceError("packed script 括号未闭合")


def _split_top_level(text: str, delimiter: str) -> list[str]:
    result: list[str] = []
    current = ""
    in_string = False
    string_char = ""
    depth = 0
    i = 0
    while i < len(text):
        ch = text[i]
        if in_string:
            current += ch
            if ch == "\\" and i + 1 < len(text):
                current += text[i + 1]
                i += 2
                continue
            if ch == string_char:
                in_string = False
            i += 1
            continue
        if ch in ('"', "'", "`"):
            in_string = True
            string_char = ch
            current += ch
        elif ch in "([{":
            depth += 1
            current += ch
        elif ch in ")]}":
            depth -= 1
            current += ch
        elif ch == delimiter and depth == 0:
            result.append(current)
            current = ""
        else:
            current += ch
        i += 1
    if current:
        result.append(current)
    return result


def _unquote_string(trimmed: str, quote: str) -> str:
    out = ""
    i = 1
    while i < len(trimmed) - 1:
        ch = trimmed[i]
        if ch == "\\" and i + 1 < len(trimmed) - 1:
            nxt = trimmed[i + 1]
            mapping = {
                "n": "\n",
                "t": "\t",
                "r": "\r",
                "b": "\b",
                "f": "\f",
                "v": "\v",
                "0": "\0",
                "\\": "\\",
                '"': '"',
                "'": "'",
                "`": "`",
            }
            out += mapping.get(nxt, nxt)
            i += 2
            continue
        out += ch
        i += 1
    return out


def parse_packed_args(call: str) -> tuple[str, int, int, str]:
    sig_end = call.find("{")
    if sig_end == -1:
        raise SourceError("无法找到 packed JS 函数体")
    brace_depth = 1
    body_end = sig_end + 1
    while body_end < len(call):
        ch = call[body_end]
        if ch == "{":
            brace_depth += 1
        elif ch == "}":
            brace_depth -= 1
            if brace_depth == 0:
                break
        body_end += 1
    if brace_depth != 0:
        raise SourceError("packed JS 括号不匹配")
    args_str = call[body_end + 1 :].strip()
    if not args_str.startswith("(") or not args_str.endswith(")"):
        raise SourceError("packed JS 参数格式异常")
    args = _split_top_level(args_str[1:-1], ",")
    if len(args) < 4:
        raise SourceError("packed JS 参数不足")

    def parse_str(arg: str) -> str:
        t = arg.strip()
        if not t or t[0] not in ('"', "'", "`"):
            raise SourceError(f"参数不是字符串: {t[:80]}")
        return _unquote_string(t, t[0])

    return parse_str(args[0]), int(args[1].strip()), int(args[2].strip()), parse_str(args[3])


def _get_base_value(alphabet: str, char: str) -> int:
    idx = alphabet.find(char)
    if idx == -1:
        raise SourceError(f"Invalid base64 char: {char}")
    return idx


def _lz_decompress(length: int, reset_value: int, get_next_value):
    dictionary: list[str] = []
    result: list[str] = []
    data = {"val": get_next_value(0), "position": reset_value, "index": 1}
    enlarge_in = 4
    dict_size = 4
    num_bits = 3
    entry = ""
    c = 0
    bits = 0
    maxpower = 2 ** 2
    power = 1

    for i in range(3):
        dictionary.append(str(i))

    while power != maxpower:
        resb = data["val"] & data["position"]
        data["position"] >>= 1
        if data["position"] == 0:
            data["position"] = reset_value
            data["val"] = get_next_value(data["index"])
            data["index"] += 1
        bits |= (1 if resb > 0 else 0) * power
        power <<= 1

    if bits == 0:
        bits = 0
        maxpower = 2 ** 8
        power = 1
        while power != maxpower:
            resb = data["val"] & data["position"]
            data["position"] >>= 1
            if data["position"] == 0:
                data["position"] = reset_value
                data["val"] = get_next_value(data["index"])
                data["index"] += 1
            bits |= (1 if resb > 0 else 0) * power
            power <<= 1
        c = bits
    elif bits == 1:
        bits = 0
        maxpower = 2 ** 16
        power = 1
        while power != maxpower:
            resb = data["val"] & data["position"]
            data["position"] >>= 1
            if data["position"] == 0:
                data["position"] = reset_value
                data["val"] = get_next_value(data["index"])
                data["index"] += 1
            bits |= (1 if resb > 0 else 0) * power
            power <<= 1
        c = bits
    elif bits == 2:
        return ""
    else:
        return ""

    dictionary.append(str(c))
    w = str(c)
    result.append(str(c))

    while True:
        if data["index"] > length:
            return ""
        bits = 0
        maxpower = 2 ** num_bits
        power = 1
        while power != maxpower:
            resb = data["val"] & data["position"]
            data["position"] >>= 1
            if data["position"] == 0:
                data["position"] = reset_value
                data["val"] = get_next_value(data["index"])
                data["index"] += 1
            bits |= (1 if resb > 0 else 0) * power
            power <<= 1

        if bits == 0:
            bits = 0
            maxpower = 2 ** 8
            power = 1
            while power != maxpower:
                resb = data["val"] & data["position"]
                data["position"] >>= 1
                if data["position"] == 0:
                    data["position"] = reset_value
                    data["val"] = get_next_value(data["index"])
                    data["index"] += 1
                bits |= (1 if resb > 0 else 0) * power
                power <<= 1
            dictionary.append(str(bits))
            c = dict_size
            dict_size += 1
            enlarge_in -= 1
        elif bits == 1:
            bits = 0
            maxpower = 2 ** 16
            power = 1
            while power != maxpower:
                resb = data["val"] & data["position"]
                data["position"] >>= 1
                if data["position"] == 0:
                    data["position"] = reset_value
                    data["val"] = get_next_value(data["index"])
                    data["index"] += 1
                bits |= (1 if resb > 0 else 0) * power
                power <<= 1
            dictionary.append(str(bits))
            c = dict_size
            dict_size += 1
            enlarge_in -= 1
        elif bits == 2:
            return "".join(result)
        else:
            c = bits

        if enlarge_in == 0:
            enlarge_in = 2 ** num_bits
            num_bits += 1

        if c < len(dictionary):
            entry = dictionary[c]
        elif c == dict_size and isinstance(w, str):
            entry = w + w[0]
        else:
            return ""

        result.append(entry)
        dictionary.append(w + entry[0])
        dict_size += 1
        enlarge_in -= 1
        w = entry

        if enlarge_in == 0:
            enlarge_in = 2 ** num_bits
            num_bits += 1


_KEY_STR_BASE64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/="


def lzstring_decompress_from_base64(input_text: str) -> str | None:
    if not input_text:
        return ""
    clean = re.sub(r"[^A-Za-z0-9+/=]", "", input_text)
    return _lz_decompress(
        len(clean),
        32,
        lambda index: _get_base_value(_KEY_STR_BASE64, clean[index]),
    )


_PACKER_CHARS = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"


def _to_packer_base(n: int, radix: int) -> str:
    if n == 0:
        return "0"
    out = ""
    num = n
    while num > 0:
        remainder = num % radix
        char = chr(remainder + 29) if remainder > 35 else _PACKER_CHARS[remainder]
        out = char + out
        num //= radix
    return out


def unpack_packer(template: str, words: list[str], base: int) -> str:
    """漫画柜专用 Packer 还原。"""
    count = len(words)
    mapping: dict[str, str] = {}
    for i in range(count):
        key = _to_packer_base(i, base)
        mapping[key] = words[i] or key
    result = template
    for i in range(count - 1, -1, -1):
        key = _to_packer_base(i, base)
        value = mapping[key]
        if value == key:
            continue
        result = re.sub(rf"\b{re.escape(key)}\b", lambda _m: value, result)
    return result


def decode_manhuagui_image_urls(html: str) -> list[str]:
    """漫画柜阅读页 -> 图片 URL 列表。"""
    script = None
    for m in re.finditer(r"<script[^>]*>([\s\S]*?)</script>", html, re.I):
        if "function(p,a,c,k,e,d)" in m.group(1):
            script = m.group(1)
            break
    if not script:
        raise SourceError("未找到 packed script")

    b64_match = re.search(
        r"""['"]([A-Za-z0-9+/=]{200,})['"]\s*(?:\[|\.splice)""", script
    )
    if not b64_match:
        raise SourceError("未找到 base64 字符串")
    base64_str = b64_match.group(1)

    decoded = lzstring_decompress_from_base64(base64_str)
    if not decoded:
        raise SourceError("LZString 解压失败")
    words = decoded.split("|")

    pack_match = re.search(
        r"""}\('((?:\\.|[^'\\])*)'\s*,\s*(\d+)\s*,\s*(\d+)\s*,""", script
    )
    if not pack_match:
        pack_match = re.search(
            r'}\("((?:\\.|[^"\\])*)"\s*,\s*(\d+)\s*,\s*(\d+)\s*,"', script
        )
    if not pack_match:
        raise SourceError("未找到 Packer 模板")
    template = pack_match.group(1).replace("\\'", "'").replace('\\"', '"')
    base = int(pack_match.group(2))

    unpacked = unpack_packer(template, words, base)
    json_match = re.search(r"SMH\.imgData\((\{[\s\S]*?\})\)\.preInit\(\)", unpacked)
    if not json_match:
        raise SourceError("未找到 SMH.imgData JSON")
    data = json.loads(json_match.group(1))

    path = str(data.get("path") or "")
    cid = int(data.get("cid") or 0)
    sl = data.get("sl") or {}
    md5_value = str(sl.get("m") or "")
    files = data.get("files") or []
    urls: list[str] = []
    for file_name in files:
        base_file = re.sub(r"\.webp$", "", str(file_name), flags=re.I)
        urls.append(
            f"https://i.hamreus.com{path}{base_file}?cid={cid}&md5={md5_value}"
        )
    return urls


# ---------------------------------------------------------------- 如漫画


RU_MANHUA_IMAGE_KEYS = [
    "smkhy258",
    "smkd95fv",
    "md496952",
    "cdcsdwq",
    "vbfsa256",
    "cawf151c",
    "cd56cvda",
    "8kihnt9",
    "dso15tlo",
    "5ko6plhy",
]


def decrypt_ru_manhua_images(cipher_b64: str, reader_id: int) -> list[str]:
    if not (0 <= reader_id < len(RU_MANHUA_IMAGE_KEYS)):
        raise SourceError(f"不支持的 reader data-id: {reader_id}")
    key = RU_MANHUA_IMAGE_KEYS[reader_id].encode("utf-8")
    cipher = base64.b64decode(cipher_b64)
    xored = bytes(b ^ key[i % len(key)] for i, b in enumerate(cipher))
    json_text = base64.b64decode(xored).decode("utf-8")
    return json.loads(json_text)


def extract_ru_manhua_cipher(html: str) -> str:
    call = extract_packed_script(html)
    _, _, _, _ = parse_packed_args(call)
    # 重新完整解析，parse_packed_args 返回 (p, a, c, k)
    p, a, c, k = parse_packed_args(call)
    decoded = unpack_dean_edwards(p, a, c, k)
    match = re.search(r'(?:var\s+)?__c0rst96\s*=\s*"([^"]*)"', decoded)
    if not match:
        raise SourceError("未找到图片加密数据 __c0rst96")
    return match.group(1)


def extract_ru_manhua_reader_id(html: str) -> int:
    match = re.search(r'class="readerContainer"[^>]*data-id="(\d+)"', html)
    if not match:
        raise SourceError("未找到 readerContainer data-id")
    return int(match.group(1))


class RuManHuaSource(SourceAdapter):
    name = "rumanhua"
    label = "如漫画"
    BASE_URL = "http://www.rumanhua2.com"

    def __init__(self, timeout: float = 30.0):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": UA_CHROME,
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,"
                    "image/avif,image/webp,image/apng,*/*;q=0.8"
                ),
                "Accept-Language": "zh-CN,zh;q=0.9",
            }
        )

    def search(self, keyword: str) -> list[SourceResult]:
        def search_once(query: str) -> list[SourceResult]:
            resp = _http_post(
                self.session,
                f"{self.BASE_URL}/s",
                headers={"Content-Type": "application/x-www-form-urlencoded", "Referer": self.BASE_URL},
                data={"k": query},
                timeout=self.timeout,
            )
            soup = BeautifulSoup(resp.text, "html.parser")
            items: list[SourceResult] = []
            for box in soup.select(".col-auto"):
                link = box.find("a")
                if not link:
                    continue
                href = link.get("href") or ""
                comic_id = href.strip("/").strip()
                if not comic_id:
                    continue
                img = box.find("img")
                cover = (img.get("data-src") or img.get("src") or "") if img else ""
                title_el = box.select_one(".e-title")
                title = (title_el.get_text(strip=True) if title_el else "") or (
                    img.get("alt") if img else ""
                )
                items.append(SourceResult(id=comic_id, title=title, cover=cover))
            return items

        items = search_once(keyword)
        if items:
            return items
        # 如漫画对过长关键词经常返回 0 结果，截短重试
        for length in (10, 8, 6):
            if len(keyword) <= length:
                continue
            items = search_once(keyword[:length])
            if items:
                break
        seen: set[str] = set()
        dedup: list[SourceResult] = []
        for item in items:
            if item.id not in seen:
                seen.add(item.id)
                dedup.append(item)
        return dedup

    def get_comic(self, result: SourceResult) -> ComicInfo:
        comic_id = result.id
        html = _http_get(
            self.session,
            f"{self.BASE_URL}/{comic_id}/",
            headers={"Referer": self.BASE_URL},
            timeout=self.timeout,
        ).text
        soup = BeautifulSoup(html, "html.parser")
        title_el = soup.select_one("h1.name_mh")
        title = title_el.get_text(strip=True) if title_el else result.title

        page_chapters: list[tuple[str, str]] = []
        for a in soup.select(".chapterlistload a"):
            href = a.get("href") or ""
            m = re.search(r"/([^/]+)\.html(?:\?.*)?$", href)
            if not m:
                continue
            name = (a.find("li").get_text(strip=True) if a.find("li") else "") or a.get_text(strip=True)
            if name:
                page_chapters.append((m.group(1), name))

        more_chapters: list[tuple[str, str]] = []
        try:
            more_resp = _http_post(
                self.session,
                f"{self.BASE_URL}/morechapter",
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Referer": f"{self.BASE_URL}/{comic_id}/",
                },
                data={"id": comic_id},
                timeout=self.timeout,
            )
            more_json = more_resp.json()
            if str(more_json.get("code")) == "200":
                for row in more_json.get("data") or []:
                    cid = str(row.get("chapterid") or "").strip()
                    cname = str(row.get("chaptername") or "").strip()
                    if cid and cname:
                        more_chapters.append((cid, cname))
        except Exception:
            pass

        merged: list[tuple[str, str]] = []
        seen: set[str] = set()
        for cid, cname in list(page_chapters) + list(more_chapters):
            if cid not in seen:
                seen.add(cid)
                merged.append((cid, cname))
        if not merged:
            raise SourceError("获取章节列表失败")
        merged.reverse()
        chapters = [
            ChapterItem(id=cid, name=cname, order=index + 1)
            for index, (cid, cname) in enumerate(merged)
        ]
        cover_url = ""
        meta_cover = soup.select_one('meta[property="og:image"]')
        if meta_cover and meta_cover.get("content"):
            cover_url = str(meta_cover["content"]).strip()
        return ComicInfo(id=comic_id, title=title, cover=cover_url, chapters=chapters)

    def get_chapter_images(self, comic: ComicInfo, chapter: ChapterItem) -> list[str]:
        html = _http_get(
            self.session,
            f"{self.BASE_URL}/{comic.id}/{chapter.id}.html",
            headers={"Referer": f"{self.BASE_URL}/{comic.id}/"},
            timeout=self.timeout,
        ).text
        cipher = extract_ru_manhua_cipher(html)
        reader_id = extract_ru_manhua_reader_id(html)
        return decrypt_ru_manhua_images(cipher, reader_id)

    def image_headers(self, url: str) -> dict[str, str]:
        return {"Referer": self.BASE_URL + "/"}


# ---------------------------------------------------------------- 漫画柜


class ManHuaGuiSource(SourceAdapter):
    name = "manhuagui"
    label = "漫画柜"
    BASE_URL = "https://tw.manhuagui.com"

    def __init__(self, timeout: float = 30.0):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": UA_CHROME,
                "Referer": self.BASE_URL + "/",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            }
        )

    def search(self, keyword: str) -> list[SourceResult]:
        encoded = urllib.parse.quote(keyword)
        html = _http_get(
            self.session, f"{self.BASE_URL}/s/{encoded}.html", timeout=self.timeout
        ).text
        soup = BeautifulSoup(html, "html.parser")
        items: list[SourceResult] = []
        for li in soup.select(".book-result > ul > li"):
            cover = li.select_one(".book-cover a.bcover")
            if not cover:
                continue
            href = cover.get("href") or ""
            m = re.search(r"/comic/(\d+)", href)
            if not m:
                continue
            bid = m.group(1)
            title = cover.get("title") or ""
            if not title:
                dt_a = li.select_one("dt a")
                title = dt_a.get_text(strip=True) if dt_a else ""
            img = cover.find("img")
            cover_url = img.get("src") or "" if img else ""
            items.append(SourceResult(id=bid, title=title, cover=cover_url))
        return items

    def get_comic(self, result: SourceResult) -> ComicInfo:
        bid = result.id
        html = _http_get(
            self.session, f"{self.BASE_URL}/comic/{bid}/", timeout=self.timeout
        ).text
        soup = BeautifulSoup(html, "html.parser")
        title_el = soup.select_one(".book-detail .book-title h1")
        title = title_el.get_text(strip=True) if title_el else result.title

        chapters: list[ChapterItem] = []
        order = 1
        for ul in soup.select('[id^="chapter-list-"] ul'):
            group: list[ChapterItem] = []
            for a in ul.select("li a"):
                href = a.get("href") or ""
                m = re.search(r"/comic/(\d+)(?:/(\d+)\.html)?", href)
                if not m or not m.group(2):
                    continue
                cid = m.group(2)
                name = a.get("title") or a.get_text(strip=True)
                group.append(ChapterItem(id=cid, name=name, order=0))
            group.reverse()
            for item in group:
                item.order = order
                order += 1
                chapters.append(item)
        cover_url = ""
        cover_img = soup.select_one(".book-cover img")
        if cover_img:
            cover_url = str(cover_img.get("src") or "").strip()
        return ComicInfo(id=bid, title=title, cover=cover_url, chapters=chapters)

    def get_chapter_images(self, comic: ComicInfo, chapter: ChapterItem) -> list[str]:
        html = _http_get(
            self.session,
            f"{self.BASE_URL}/comic/{comic.id}/{chapter.id}.html",
            timeout=self.timeout,
        ).text
        return decode_manhuagui_image_urls(html)

    def image_headers(self, url: str) -> dict[str, str]:
        return {"Referer": self.BASE_URL + "/"}


# ---------------------------------------------------------------- 拷贝漫画


COPY_API_DOMAINS: dict[str, str] = {
    "intl": "https://api.mangacopy.com/api/v3",
    "intl1": "https://api.copy2000.online/api/v3",
    "cn1": "https://mapi.copy20.com/api/v3",
    "cn2": "https://mapi.copy2000.site/api/v3",
    "cn3": "https://api.2025copy.com/api/v3",
    "cnnew": "https://api.2026copy.com/api/v3",
    "hot1": "https://mapi.hotmangasd.com/api/v3",
    "hot2": "https://api.manga2025.com/api/v3",
    "hot3": "https://mapi.hotmangasf.com/api/v3",
    "hot4": "https://mapi.hotmangasg.com/api/v3",
    "hot5": "https://mapi.elfgjfghkk.club/api/v3",
    "hot6": "https://mapi.fgjfghkk.club/api/v3",
    "hot7": "https://mapi.fgjfghkkcenter.club/api/v3",
}


def _is_hot_copy_api(api_base: str) -> bool:
    host = (urllib.parse.urlparse(api_base).hostname or "").lower()
    return "hotmanga" in host or host == "api.manga2025.com" or "fgjfghkk" in host


class CopyComicSource(SourceAdapter):
    name = "copy"
    label = "拷贝漫画"

    def __init__(
        self,
        api_choice: str = "hot2",
        api_base: str = "",
        platform: str = "1",
        timeout: float = 30.0,
    ):
        self.api_base = (api_base or COPY_API_DOMAINS.get(api_choice, COPY_API_DOMAINS["hot2"])).rstrip("/")
        self.platform = platform
        self.timeout = timeout
        self.session = requests.Session()

    def _headers(self) -> dict[str, str]:
        hot = _is_hot_copy_api(self.api_base)
        version = "2025.02.12" if hot else "2025.05.09"
        headers: dict[str, str] = {
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9,zh-TW;q=0.8,zh;q=0.7",
            "version": version,
            "sec-fetch-dest": "document",
            "sec-fetch-mode": "navigate",
            "sec-fetch-site": "same-origin",
            "sec-fetch-user": "?1",
            "upgrade-insecure-requests": "1",
            "User-Agent": UA_CHROME,
        }
        if hot:
            headers.update({"Origin": "https://m.relamanhua.org", "webp": "1"})
        else:
            headers.update({"Origin": "https://2025copy.com", "region": "0", "webp": "0"})
        if self.platform:
            headers["platform"] = self.platform
        return headers

    def _get_json(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        resp = _http_get(
            self.session,
            f"{self.api_base}{path}",
            headers=self._headers(),
            params=params,
            timeout=self.timeout,
        )
        try:
            data = resp.json()
        except ValueError as exc:
            raise SourceError(f"API 返回非 JSON: {resp.text[:200]}") from exc
        if int(data.get("code") or 0) != 200:
            raise SourceError(str(data.get("message") or "API code != 200"))
        return data

    def search(self, keyword: str) -> list[SourceResult]:
        data = self._get_json(
            "/search/comic",
            {
                "limit": "20",
                "offset": "0",
                "q": keyword,
                "q_type": "",
                "platform": self.platform,
            },
        )
        items = []
        for row in (data.get("results") or {}).get("list") or []:
            comic_id = str(row.get("path_word") or "").strip()
            if comic_id:
                items.append(
                    SourceResult(
                        id=comic_id,
                        title=str(row.get("name") or "").strip(),
                        cover=str(row.get("cover") or "").strip(),
                    )
                )
        return items

    def get_comic(self, result: SourceResult) -> ComicInfo:
        comic_id = result.id
        detail = self._get_json(
            f"/comic2/{urllib.parse.quote(comic_id)}", {"platform": self.platform}
        )
        results = detail.get("results") or {}
        comic = results.get("comic") or {}
        title = str(comic.get("name") or "").strip() or result.title
        cover_url = str(comic.get("cover") or "").strip() or result.cover
        groups_value = results.get("groups") or comic.get("groups") or {}
        if isinstance(groups_value, list):
            groups = groups_value
        elif isinstance(groups_value, dict):
            groups = list(groups_value.values())
        else:
            groups = []

        chapters: list[ChapterItem] = []
        order = 1
        for group in groups:
            if not isinstance(group, dict):
                continue
            group_path = str(group.get("path_word") or "").strip()
            group_name = str(group.get("name") or "").strip()
            if not group_path:
                continue
            offset = 0
            while True:
                data = self._get_json(
                    f"/comic/{urllib.parse.quote(comic_id)}/group/{urllib.parse.quote(group_path)}/chapters",
                    {"limit": "500", "offset": str(offset)},
                )
                rows = (data.get("results") or {}).get("list") or []
                for item in rows:
                    uuid = str(item.get("uuid") or "").strip()
                    if not uuid:
                        continue
                    chapter_name = str(item.get("name") or "").strip() or f"第{order}话"
                    display = f"{group_name} - {chapter_name}" if group_name else chapter_name
                    chapters.append(
                        ChapterItem(
                            id=uuid,
                            name=display,
                            order=order,
                            extra={"group_path": group_path, "size": int(item.get("size") or 0)},
                        )
                    )
                    order += 1
                total = int((data.get("results") or {}).get("total") or len(rows))
                if offset + len(rows) >= total or not rows:
                    break
                offset += len(rows)
        return ComicInfo(id=comic_id, title=title, cover=cover_url, chapters=chapters)

    def get_chapter_images(self, comic: ComicInfo, chapter: ChapterItem) -> list[str]:
        hot = _is_hot_copy_api(self.api_base)
        paths = (["chapter", "chapter2"] if hot else ["chapter2", "chapter"])
        last_error: Exception | None = None
        for path in paths:
            try:
                data = self._get_json(
                    f"/comic/{urllib.parse.quote(comic.id)}/{path}/{urllib.parse.quote(chapter.id)}",
                    {"platform": self.platform},
                )
                chapter_node = (data.get("results") or {}).get("chapter") or {}
                contents = [
                    str(item.get("url") or "").strip()
                    for item in (chapter_node.get("contents") or [])
                    if isinstance(item, dict) and str(item.get("url") or "").strip()
                ]
                words_raw = chapter_node.get("words")
                words = (
                    [int(n) for n in words_raw if isinstance(n, (int, float)) and not isinstance(n, bool)]
                    if isinstance(words_raw, list)
                    else []
                )
                return sort_copy_images(contents, words)
            except SourceError as exc:
                last_error = exc
                if "404" not in str(exc):
                    raise
        raise SourceError(f"章节内容不可用: {last_error}")


def sort_copy_images(contents: list[str], words: list[int]) -> list[str]:
    urls = [u for u in contents if u]
    if not urls:
        return urls
    if len(words) != len(urls):
        return urls
    mapped = []
    for index, (url, order) in enumerate(zip(urls, words)):
        if url and isinstance(order, (int, float)) and not isinstance(order, bool):
            mapped.append((order, index, url))
    if not mapped:
        return urls
    mapped.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in mapped]


# ---------------------------------------------------------------- 再漫画


class ZaiManHuaSource(SourceAdapter):
    name = "zmh"
    label = "再漫画"
    API_BASE = "https://v4api.zaimanhua.com/app/v1"
    APP_VERSION = "2.3.4"
    APP_CHANNEL = "101_01_01_000"
    TOKEN_FILE = Path(__file__).resolve().parent / "zmh_token.json"
    TOKEN_TTL = 30 * 24 * 3600

    _DEVICE_PROFILES = [
        ("Xiaomi", ["2301C", "2301A", "2302B", "2303D", "2303G", "2304R"]),
        ("samsung", ["SM-S9180", "SM-S9260", "SM-A5560", "SM-A7360", "SM-S9210"]),
        ("OnePlus", ["CPH2581", "CPH2609", "CPH2451", "CPH2449", "CPH2493"]),
        ("vivo", ["V2337A", "V2366A", "V2358A", "V2318A", "V2407A"]),
        ("HUAWEI", ["NOH-AN00", "NOH-AL10", "NOH-NX9", "NOH-LX9", "NOH-TL00"]),
    ]

    def __init__(self, timeout: float = 30.0, account: str = "", password: str = ""):
        self.timeout = timeout
        self.account = (account or "").strip()
        self.password = password or ""
        self._token = ""
        self._token_expires_at = 0.0
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})
        self._ua = self._build_ua()
        self._load_token()

    def _load_token(self) -> None:
        try:
            if not self.TOKEN_FILE.exists():
                return
            data = json.loads(self.TOKEN_FILE.read_text(encoding="utf-8"))
            token = str(data.get("token") or "").strip()
            expires_at = float(data.get("expires_at") or 0)
            if token and expires_at > time.time():
                self._token = token
                self._token_expires_at = expires_at
        except Exception:
            pass

    def _save_token(self, token: str) -> None:
        try:
            self.TOKEN_FILE.write_text(
                json.dumps(
                    {
                        "token": token,
                        "expires_at": time.time() + self.TOKEN_TTL,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except OSError:
            pass

    def login(self) -> bool:
        """再漫画账号密码登录，成功后保存 token。"""
        if not self.account or not self.password:
            return False
        try:
            encrypted_pwd = hashlib.md5(self.password.encode("utf-8")).hexdigest()
            resp = _http_post(
                self.session,
                "https://account-api.zaimanhua.com/v1/login/passwd",
                headers={
                    "User-Agent": self._ua,
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
                },
                data={"username": self.account, "passwd": encrypted_pwd},
                timeout=self.timeout,
            )
            data = resp.json()
            errno = data.get("errno")
            if int(errno if errno is not None else -1) != 0:
                raise SourceError(str(data.get("errmsg") or "再漫画登录失败"))
            user = (data.get("data") or {}).get("user") or {}
            token = str(user.get("token") or "").strip()
            if not token:
                raise SourceError("再漫画登录成功但未返回 token")
            self._token = token
            self._token_expires_at = time.time() + self.TOKEN_TTL
            self._save_token(token)
            return True
        except Exception as exc:
            print(f"  [warn] 再漫画登录失败: {exc}")
            return False

    def ensure_login(self) -> bool:
        """已有未过期 token 则直接使用，否则重新登录。"""
        if self._token and self._token_expires_at > time.time():
            return True
        return self.login()

    def _build_ua(self) -> str:
        brand, models = random.choice(self._DEVICE_PROFILES)
        model = random.choice(models)
        android = random.choice(["10", "11", "12", "13", "14", "15"])
        webkit = f"537.{random.randint(30, 38)}"
        chrome_major = random.randint(108, 136)
        build = random.choice(["QP1A", "SP1A", "TP1A", "UP1A", "AP1A"])
        build_id = f"{build}.{random.randint(200000, 999999)}.{random.randint(1, 99)}"
        return (
            f"Mozilla/5.0 (Linux; Android {android}; {model} {brand}; Build/{build_id}) "
            f"AppleWebKit/{webkit} (KHTML, like Gecko) Chrome/{chrome_major}.0.0.0 "
            f"Mobile Safari/{webkit}"
        )

    def _params(self) -> dict[str, str]:
        return {
            "platform": "android",
            "timestamp": str(int(time.time())),
            "_v": self.APP_VERSION,
            "_c": self.APP_CHANNEL,
        }

    def _get_json(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        merged = {**params, **self._params()}
        headers = {"User-Agent": self._ua}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        resp = _http_get(
            self.session,
            f"{self.API_BASE}{path}",
            headers=headers,
            params=merged,
            timeout=self.timeout,
        )
        try:
            data = resp.json()
        except ValueError as exc:
            raise SourceError(f"再漫画 API 非 JSON: {resp.text[:200]}") from exc
        if data.get("errno") != 0:
            raise SourceError(str(data.get("errmsg") or "再漫画 API 错误"))
        return data

    def search(self, keyword: str) -> list[SourceResult]:
        data = self._get_json(
            "/search/index",
            {"keyword": keyword, "page": "1", "sort": "0", "size": "20"},
        )
        items = []
        for row in (data.get("data") or {}).get("list") or []:
            comic_id = str(row.get("comic_id") or row.get("id") or "").strip()
            if comic_id:
                items.append(
                    SourceResult(
                        id=comic_id,
                        title=str(row.get("title") or "").strip(),
                        cover=str(row.get("cover") or "").strip(),
                    )
                )
        return items

    def get_comic(self, result: SourceResult) -> ComicInfo:
        data = self._get_json(
            f"/comic/detail/{urllib.parse.quote(result.id)}", {}
        )
        node = (data.get("data") or {}).get("data") or {}
        title = str(node.get("title") or "").strip() or result.title
        cover_url = str(node.get("cover") or "").strip() or result.cover
        chapters: list[ChapterItem] = []
        order = 1
        for group in node.get("chapters") or []:
            group_title = str(group.get("title") or "").strip()
            for row in group.get("data") or []:
                cid = str(row.get("chapter_id") or "").strip()
                if not cid:
                    continue
                cname = str(row.get("chapter_title") or "").strip() or f"第{order}话"
                display = f"{group_title}-{cname}" if group_title else cname
                chapters.append(
                    ChapterItem(
                        id=cid,
                        name=display,
                        order=order,
                        extra={
                            "is_fee": bool(row.get("is_fee")),
                            "can_read": row.get("canRead") is not False,
                        },
                    )
                )
                order += 1
        chapters.reverse()
        for index, chapter in enumerate(chapters, start=1):
            chapter.order = index
        return ComicInfo(id=result.id, title=title, cover=cover_url, chapters=chapters)

    def get_chapter_images(self, comic: ComicInfo, chapter: ChapterItem) -> list[str]:
        path = f"/comic/chapter/{urllib.parse.quote(comic.id)}/{urllib.parse.quote(chapter.id)}"
        data = self._get_json(path, {})
        node = (data.get("data") or {}).get("data") or {}
        if node.get("canRead") is False:
            if self._token and self._token_expires_at > time.time():
                raise SourceError("章节需要登录或权限不足")
            if not self.login():
                raise SourceError("章节需要登录或权限不足（未配置再漫画账号）")
            data = self._get_json(path, {})
            node = (data.get("data") or {}).get("data") or {}
            if node.get("canRead") is False:
                raise SourceError("章节需要登录或权限不足")
        images = node.get("page_url_hd") or node.get("page_url") or []
        urls = [str(u).strip() for u in images if str(u).strip()]
        if not urls:
            raise SourceError("章节没有可用图片")
        return urls


# ---------------------------------------------------------------- 包子漫画


class BaoZiMhSource(SourceAdapter):
    name = "baozimh"
    label = "包子漫画"
    BASE_URL = "https://www.baozimh.com"
    LAST_CHAPTER_MARK = "/last_chapter"
    BYPASS_HOSTS = [
        "appgb-vdkr.baozimh.com",
        "appgb1-vdkr.baozimh.com",
        "appgb2-vdkr.baozimh.com",
        "app1-vdkr.baozimh.com",
        "app2-vdkr.baozimh.com",
    ]
    APP_UA = "baozimh_android/1.0.31/gb/adset"
    APP_VERSION = "1.0.31"
    APP_ID = "cn.sts.xiaoyun.ordermeals"
    DEVICE_ID = "BE2A.250530.026.F3"
    DEVICE_CODE = "2c712c6ba4e95a9f4157f94e1794a86c"

    def __init__(self, timeout: float = 30.0):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": self.APP_UA,
                "Referer": self.BASE_URL + "/",
            }
        )

    def search(self, keyword: str) -> list[SourceResult]:
        html = _http_get(
            self.session, f"{self.BASE_URL}/search", params={"q": keyword}, timeout=self.timeout
        ).text
        soup = BeautifulSoup(html, "html.parser")
        items: list[SourceResult] = []
        for a in soup.select("a.comics-card__poster, div.classify-items a"):
            href = a.get("href") or ""
            if not href.startswith("/comic/"):
                continue
            comic_id = href[len("/comic/") :].strip("/")
            if not comic_id:
                continue
            title = a.get("title") or ""
            if not title:
                img = a.find("img")
                title = img.get("alt") if img else ""
            cover = ""
            img = a.find("amp-img") or a.find("img")
            if img:
                cover = img.get("src") or img.get("data-src") or ""
            items.append(SourceResult(id=comic_id, title=title, cover=cover))
        return items

    def get_comic(self, result: SourceResult) -> ComicInfo:
        html = _http_get(
            self.session, f"{self.BASE_URL}/comic/{result.id}", timeout=self.timeout
        ).text
        soup = BeautifulSoup(html, "html.parser")
        h1 = soup.select_one("h1.comics-detail__title")
        title = h1.get_text(strip=True) if h1 else result.title
        cover_url = ""
        meta_cover = soup.select_one('meta[property="og:image"]')
        if meta_cover and meta_cover.get("content"):
            cover_url = str(meta_cover["content"]).strip()
        if not cover_url:
            amp = soup.select_one("div.pure-g div > amp-img")
            if amp:
                cover_url = str(amp.get("src") or "").strip()

        raw: list[tuple[str, str]] = []
        for a in soup.select(".comics-chapters a[href]"):
            name = a.get_text(strip=True)
            href = a.get("href") or ""
            if href:
                raw.append((name, self._to_quick_chapter_path(href)))
        if not raw:
            raise SourceError("未找到章节列表")

        dedup: list[tuple[str, str]] = []
        seen: set[str] = set()
        for name, path in raw:
            key = f"{name}@@{path}"
            if key not in seen:
                seen.add(key)
                dedup.append((name, path))

        def section_chapter(path: str) -> tuple[int, int]:
            m = re.search(r"/comic/chapter/[^/]+/(\d+)_(\d+)\.html", path)
            if not m:
                return (0, 0)
            return int(m.group(1)), int(m.group(2))

        dedup.sort(key=lambda item: section_chapter(item[1]), reverse=True)
        dedup.reverse()
        chapters = [
            ChapterItem(
                id=path,
                name=name,
                order=index + 1,
                extra={"is_last": index == 0},
            )
            for index, (name, path) in enumerate(dedup)
        ]
        return ComicInfo(id=result.id, title=title, cover=cover_url, chapters=chapters)

    def _to_quick_chapter_path(self, raw_path: str) -> str:
        parsed = urllib.parse.urlparse(raw_path)
        query = urllib.parse.parse_qs(parsed.query)
        comic_id = query.get("comic_id", [""])[0]
        section = query.get("section_slot", [""])[0]
        chapter = query.get("chapter_slot", [""])[0]
        if comic_id and section and chapter:
            return f"/comic/chapter/{comic_id}/{section}_{chapter}.html"
        return raw_path

    def _build_chapter_request(self, chapter_path: str):
        is_last = chapter_path.endswith(self.LAST_CHAPTER_MARK)
        if not is_last:
            url = chapter_path
            if url.startswith("/"):
                url = self.BASE_URL + url
            return url, {"Referer": self.BASE_URL + "/"}
        raw = chapter_path[: -len(self.LAST_CHAPTER_MARK)]
        parsed = urllib.parse.urlparse(raw)
        if not parsed.netloc:
            raw = self.BASE_URL + raw
        host = random.choice(self.BYPASS_HOSTS)
        parsed = urllib.parse.urlparse(raw)
        url = f"https://{host}/baozimhapp{parsed.path}"
        if parsed.query:
            url += "?" + parsed.query
        headers = {
            "Referer": "https://app.baozimh.com/",
            "Accept-Encoding": "gzip",
            "app-id": self.APP_ID,
            "app-version": self.APP_VERSION,
            "connection": "Keep-Alive",
            "device-code": self.DEVICE_CODE,
            "device-id": self.DEVICE_ID,
            "user-agent": self.APP_UA,
        }
        return url, headers

    def get_chapter_images(self, comic: ComicInfo, chapter: ChapterItem) -> list[str]:
        pages: list[str] = []
        current_path = chapter.id
        is_last_root = current_path.endswith(self.LAST_CHAPTER_MARK)
        visited: set[str] = set()
        while current_path not in visited:
            visited.add(current_path)
            url, headers = self._build_chapter_request(current_path)
            html = _http_get(self.session, url, headers=headers, timeout=self.timeout).text
            soup = BeautifulSoup(html, "html.parser")
            urls: list[str] = []
            if not current_path.endswith(self.LAST_CHAPTER_MARK):
                for state in soup.select('amp-state[id^="chapter"][id$="Src"] > script[type="application/json"]'):
                    try:
                        obj = json.loads(state.get_text())
                    except (ValueError, TypeError):
                        continue
                    if isinstance(obj, dict) and obj.get("url"):
                        urls.append(str(obj["url"]))
            else:
                for img in soup.select("div.chapter-img img.comic-contain__item[data-src]"):
                    src = img.get("data-src")
                    if src:
                        urls.append(src)
            for u in urls:
                if u not in pages:
                    pages.append(u)

            next_href = None
            for a in soup.select("div.next_chapter a"):
                text = a.get_text()
                if "點擊進入下一頁" in text or "点击进入下一页" in text:
                    next_href = a.get("href")
                    break
            if not next_href:
                break
            current_path = urllib.parse.urljoin(url, next_href)
            if is_last_root:
                current_path += self.LAST_CHAPTER_MARK
        return pages

    def image_headers(self, url: str) -> dict[str, str]:
        return {"Referer": self.BASE_URL + "/"}


# ---------------------------------------------------------------- 蛙漫3


class Manwa3Source(SourceAdapter):
    name = "manwa3"
    label = "蛙漫3"
    BASE_URLS = [
        "http://mseeowpm.pro",
        "http://mseeowpm1.xyz",
        "http://mseeowpm2.cc",
        "https://mseeowpma.cc",
    ]
    TOKEN_SALT = "jsdaghuiaonfyudsfnkgjdfkdd"
    API_KEY_SALT = "noiusdfy73osadjap012njdsfn"
    IMAGE_SECRET = b"my2ecret782ecret"
    UA = (
        "Mozilla/5.0 (Linux; Android 12; PGT-AN20) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36 "
        "mwa-1.1.26+1 (Android/12 HONOR/PGT-AN20)"
    )

    def __init__(self, timeout: float = 30.0):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
                "Origin": "http://mseeowpm1.xyz",
                "Referer": "http://mseeowpm1.xyz",
                "Content-Type": "application/json; charset=utf-8",
                "User-Agent": self.UA,
            }
        )

    def _api(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        last: Exception | None = None
        for base in self.BASE_URLS:
            try:
                devid = str(int(time.time() * 1000))
                token = hashlib.md5(f"{devid},{self.TOKEN_SALT}".encode()).hexdigest()
                headers = {
                    "devid": devid,
                    "x-token": token,
                }
                resp = _http_get(
                    self.session,
                    f"{base}{path}",
                    headers=headers,
                    params=params,
                    timeout=self.timeout,
                )
                payload = json.loads(resp.text)
                if not isinstance(payload, str):
                    raise SourceError(f"蛙漫3 响应格式异常: {str(payload)[:200]}")
                key = hashlib.md5(f"{devid}{self.API_KEY_SALT}".encode()).hexdigest()
                cipher = AES.new(key.encode("utf-8"), AES.MODE_ECB)
                decrypted = cipher.decrypt(base64.b64decode(payload))
                decrypted = _strip_pkcs7(decrypted)
                data = json.loads(decrypted.decode("utf-8"))
                if int(data.get("code") or 0) != 1:
                    raise SourceError(str(data.get("msg") or "蛙漫3 API 错误"))
                return data.get("data") or {}
            except Exception as exc:
                last = exc
        raise SourceError(f"蛙漫3 所有线路失败: {last}")

    def search(self, keyword: str) -> list[SourceResult]:
        data = self._api("/api/search/index", {"k": keyword, "page": "1"})
        items = []
        for row in data.get("list") or []:
            comic_id = str(row.get("id") or "").strip()
            if comic_id:
                items.append(
                    SourceResult(
                        id=comic_id,
                        title=str(row.get("name") or "").strip(),
                        cover=str(row.get("picx") or row.get("pic") or "").strip(),
                    )
                )
        return items

    def get_comic(self, result: SourceResult) -> ComicInfo:
        data = self._api("/api/detail/index", {"id": result.id})
        title = str(data.get("name") or "").strip() or result.title
        cover_url = str(data.get("picx") or data.get("pic") or result.cover or "").strip()
        chapters = [
            ChapterItem(
                id=str(row.get("id") or "").strip(),
                name=str(row.get("name") or "").strip(),
                order=int(row.get("sort") or index + 1),
            )
            for index, row in enumerate(data.get("chapter_list") or [])
            if str(row.get("id") or "").strip()
        ]
        return ComicInfo(id=result.id, title=title, cover=cover_url, chapters=chapters)

    def get_chapter_images(self, comic: ComicInfo, chapter: ChapterItem) -> list[str]:
        data = self._api(
            "/api/chapters/index", {"id": chapter.id, "img_host": "0"}
        )
        urls = [
            str(row.get("pic") or "").strip()
            for row in data.get("piclist") or []
            if str(row.get("pic") or "").strip()
        ]
        if not urls:
            raise SourceError("章节没有可用图片")
        return urls

    def decrypt_image(self, data: bytes) -> bytes:
        cipher = AES.new(self.IMAGE_SECRET, AES.MODE_CBC, self.IMAGE_SECRET)
        decrypted = cipher.decrypt(data)
        return _strip_pkcs7(decrypted)

    def force_ext(self) -> str | None:
        return ".webp"


def _strip_pkcs7(data: bytes) -> bytes:
    if not data:
        return data
    pad = data[-1]
    if 1 <= pad <= 16 and data[-pad:] == bytes([pad]) * pad:
        return data[:-pad]
    return data


# ---------------------------------------------------------------- 源列表


def build_sources(
    enabled: list[str] | None = None,
    copy_api_choice: str = "hot2",
    copy_api_base: str = "",
    platform: str = "1",
    timeout: float = 30.0,
    zmh_account: str = "",
    zmh_password: str = "",
) -> list[SourceAdapter]:
    all_sources: list[SourceAdapter] = [
        CopyComicSource(
            api_choice=copy_api_choice, api_base=copy_api_base, platform=platform, timeout=timeout
        ),
        ZaiManHuaSource(timeout=timeout, account=zmh_account, password=zmh_password),
        BaoZiMhSource(timeout=timeout),
        Manwa3Source(timeout=timeout),
        RuManHuaSource(timeout=timeout),
        ManHuaGuiSource(timeout=timeout),
    ]
    if not enabled:
        return all_sources
    wanted = {name.strip().lower() for name in enabled if name.strip()}
    return [source for source in all_sources if source.name in wanted]
