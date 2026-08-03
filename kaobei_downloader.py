#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
多源漫画批量下载器

流程：
  名单 -> 聚合搜索多个资源 -> 按“可读章节数最多”选择资源
       -> 多章节并发下载 -> 图片校验
       -> 记录成功/失败（失败写入 失败.log）-> 继续下一项

章节文件夹统一命名为 第1话、第2话 ... 第N话，
换源续传时按序号定位，已完成的章节直接跳过。

支持资源：拷贝漫画 / 再漫画 / 包子漫画 / 蛙漫3 / 如漫画 / 漫画柜
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
import shutil
import signal
import threading
import time
try:
    import msvcrt
    HAS_MSVCRT = True
except ImportError:
    HAS_MSVCRT = False
import unicodedata
from collections import deque
from concurrent.futures import (
    FIRST_COMPLETED,
    ThreadPoolExecutor,
    as_completed,
    wait,
)
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
from zhconv import convert as zh_convert

from sources import (
    ComicInfo,
    SourceAdapter,
    SourceError,
    SourceResult,
    build_sources,
)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def color(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m"


def c_ok(text: str) -> str:
    return color(text, "32")


def c_fail(text: str) -> str:
    return color(text, "31")


def c_warn(text: str) -> str:
    return color(text, "33")


def c_info(text: str) -> str:
    return color(text, "36")


def c_head(text: str) -> str:
    return color(text, "36;1")


_io_lock = threading.Lock()
_active_progress: "Progress" | None = None


def emit(text: str, end: str = "\n") -> None:
    """串行输出：先清掉进度区，再写日志，然后立即重绘进度区，避免日志被打断。"""
    with _io_lock:
        p = _active_progress
        if p is not None and Progress.rows > 0:
            sys.stdout.write(f"\033[{max(0, Progress.rows - 1)}A\033[J")
            Progress.rows = 0
        sys.stdout.write(text + end)
        if p is not None and p.has_active_slots():
            p._render_locked()
        sys.stdout.flush()


# ---------------------------------------------------------------- 名称处理


def normalize_name(value: str) -> str:
    """归一化名称：NFKC、全角转半角、繁体转简体、压缩空白。"""
    text = unicodedata.normalize("NFKC", value or "").strip()
    out: list[str] = []
    for ch in text:
        code = ord(ch)
        if ch == "\u3000":
            out.append(" ")
        elif 0xFF01 <= code <= 0xFF5E:
            out.append(chr(code - 0xFEE0))
        else:
            out.append(ch)
    text = "".join(out)
    text = zh_convert(text, "zh-cn")
    text = re.sub(r"\s+", "", text)
    return text.lower()


def match_score(item_title: str, keyword: str) -> float:
    """返回 0-1 匹配分数：完全一致最高，包含关系次之，编辑相似度最低。"""
    title = normalize_name(item_title)
    target = normalize_name(keyword)
    if not title or not target:
        return 0.0
    if title == target:
        return 1.0
    if target in title or title in target:
        return 0.9
    ratio = difflib.SequenceMatcher(None, title, target).ratio()
    return round(ratio, 4)


def find_best_match(
    results: list[SourceResult],
    keyword: str,
    threshold: float = 0.55,
) -> tuple[SourceResult, float] | None:
    best: tuple[SourceResult, float] | None = None
    for item in results:
        score = match_score(item.title, keyword)
        if score >= threshold and (best is None or score > best[1]):
            best = (item, score)
    return best


def sanitize_filename(name: str, fallback: str = "untitled") -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", name or "").strip()
    cleaned = cleaned.strip(" .")
    return cleaned or fallback


def chapter_folder_name(order: int) -> str:
    return f"第{order}话"


def count_valid_pages(
    chapter_dir: Path, expected: int, order: int | None = None, flat: bool = False
) -> int:
    """统计章节目录中已有的有效图片页数，只按序号识别，不检查后缀。"""
    valid = 0
    seen: set[str] = set()
    for path in chapter_dir.iterdir():
        if not path.is_file():
            continue
        if path.name == ".done":
            continue
        if flat:
            m = re.match(rf"^第{order}话_(\d{{3}})\.\w+$", path.name)
        else:
            m = re.match(r"^(\d{3})\.\w+$", path.name)
        if not m:
            continue
        idx = m.group(1)
        if idx in seen:
            continue
        if path.stat().st_size > 0 and detect_image_signature(path):
            valid += 1
            seen.add(idx)
    return valid


def find_page_files(chapter_dir: Path, page_no: int, order: int | None = None, flat: bool = False) -> list[Path]:
    if flat:
        pattern = f"第{order}话_{page_no:03d}.*"
    else:
        pattern = f"{page_no:03d}.*"
    return sorted(chapter_dir.glob(pattern))


def normalize_page_file(chapter_dir: Path, page_no: int, target: Path, order: int | None = None, flat: bool = False) -> Path | None:
    """把同页码的已有文件统一为目标后缀，避免 jpg/jpeg 混存导致重复下载。"""
    matches = find_page_files(chapter_dir, page_no, order=order, flat=flat)
    valid = [p for p in matches if p.is_file() and p.stat().st_size > 0]
    if not valid:
        return None
    keep = valid[0]
    if keep.resolve() == target.resolve():
        return keep
    if target.exists() and target.stat().st_size > 0:
        for p in valid:
            if p.resolve() != target.resolve():
                try:
                    p.unlink()
                except OSError:
                    pass
        return target
    try:
        keep.rename(target)
    except OSError:
        return keep
    for p in valid:
        if p.exists() and p.resolve() != target.resolve():
            try:
                p.unlink()
            except OSError:
                pass
    return target


def chapter_cache_path(chapter_dir: Path, order: int, flat: bool = False) -> Path:
    if flat:
        return chapter_dir / f".meta_{order}.json"
    return chapter_dir / ".meta.json"


def read_chapter_cache(
    chapter_dir: Path,
    order: int,
    source_name: str,
    flat: bool = False,
    ttl_seconds: float = 86400,
) -> list[str] | None:
    cache_file = chapter_cache_path(chapter_dir, order, flat=flat)
    if not cache_file.exists():
        return None
    try:
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        # 缓存对所有源通用：换源后也用它校验页数，不再重复联网
        fetched_at = float(data.get("fetched_at") or 0)
        if time.time() - fetched_at > ttl_seconds:
            return None
        urls = data.get("urls")
        if not isinstance(urls, list) or not urls:
            return None
        return [str(url) for url in urls]
    except Exception:
        return None


def write_chapter_cache(
    chapter_dir: Path,
    order: int,
    source_name: str,
    urls: list[str],
    flat: bool = False,
) -> None:
    cache_file = chapter_cache_path(chapter_dir, order, flat=flat)
    try:
        cache_file.write_text(
            json.dumps(
                {
                    "source": source_name,
                    "urls": urls,
                    "fetched_at": time.time(),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except OSError:
        pass


# ---------------------------------------------------------------- 交互控制


class ControlState:
    """全局交互控制状态：Ctrl+C 二次退出、Ctrl+F 菜单。"""

    def __init__(self) -> None:
        self.exit_requested = False
        self.skip_comic = False
        self.skip_chapter = False
        self.redo_prev = False
        self.paused = False
        self.interrupt_once = False


def install_sigint(ctrl: ControlState) -> None:
    """Ctrl+C 第一次提示，第二次真正退出。"""

    def handler(_signum, _frame):
        if ctrl.interrupt_once:
            ctrl.exit_requested = True
            raise KeyboardInterrupt
        ctrl.interrupt_once = True
        try:
            sys.stdout.write("\n[提示] 再按一次 Ctrl+C 退出；按 Ctrl+F 打开菜单\n")
            sys.stdout.flush()
        except Exception:
            pass

    signal.signal(signal.SIGINT, handler)


def reset_chapter_dir(comic_dir: Path, order: int, flat: bool = False) -> None:
    """重新下载章节前删除旧目录。"""
    if flat:
        return
    target = comic_dir / chapter_folder_name(order)
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)


# ---------------------------------------------------------------- 限流器


class RateLimiter:
    def __init__(self, max_per_minute: int):
        self.max_per_minute = max_per_minute
        self._timestamps: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        if self.max_per_minute <= 0:
            return
        while True:
            now = time.monotonic()
            with self._lock:
                while self._timestamps and now - self._timestamps[0] >= 60.0:
                    self._timestamps.popleft()
                if len(self._timestamps) < self.max_per_minute:
                    self._timestamps.append(now)
                    return
                wait = 60.0 - (now - self._timestamps[0])
            time.sleep(max(0.05, min(wait, 5.0)))


class Progress:
    """图片级进度条：固定 slot_count 行，串行渲染，日志输出后立即重绘。"""

    rows = 0

    def __init__(
        self,
        total_chapters: int,
        slot_count: int = 4,
        stall_timeout: int = 300,
    ):
        self.total_chapters = max(1, total_chapters)
        self.done_chapters = 0
        self.slot_count = max(1, slot_count)
        self.stall_timeout = max(1, stall_timeout)
        self.slots: list[dict[str, Any] | None] = [None] * self.slot_count
        self.chapter_totals: dict[int, int] = {}
        self.last_activity: dict[int, float] = {}
        self.waiting: dict[int, float] = {}
        self.lock = threading.Lock()
        self.menu_open = False

    def has_active_slots(self) -> bool:
        with self.lock:
            return any(slot is not None for slot in self.slots)

    def set_chapter_total(self, order: int, total: int) -> None:
        with _io_lock:
            with self.lock:
                self.chapter_totals[order] = max(1, total)
            self._render_locked()

    def mark_done(self, order: int) -> None:
        """跳过或失败的章节计入总进度。"""
        with _io_lock:
            with self.lock:
                self.done_chapters += 1
                self.last_activity.pop(order, None)
                self.waiting.pop(order, None)
            self._render_locked()

    def acquire_slot(self, order: int, filename: str) -> int:
        with _io_lock:
            with self.lock:
                for index in range(self.slot_count):
                    if self.slots[index] is None:
                        self.slots[index] = {
                            "order": order,
                            "filename": filename,
                            "percent": 0.0,
                        }
                        self.last_activity[order] = time.time()
                        self._render_locked()
                        return index
            return -1

    def update_slot(self, index: int, percent: float) -> None:
        with _io_lock:
            with self.lock:
                if 0 <= index < self.slot_count and self.slots[index] is not None:
                    self.slots[index]["percent"] = percent
                    order = self.slots[index]["order"]
                    self.last_activity[order] = time.time()
                self._render_locked()

    def release_slot(self, index: int) -> None:
        with _io_lock:
            with self.lock:
                if 0 <= index < self.slot_count and self.slots[index] is not None:
                    self.slots[index] = None
                self._render_locked()

    def finish_chapter(self, order: int) -> None:
        with _io_lock:
            with self.lock:
                self.done_chapters += 1
                self.last_activity.pop(order, None)
                self.waiting.pop(order, None)
                for index in range(self.slot_count):
                    if self.slots[index] is not None and self.slots[index]["order"] == order:
                        self.slots[index] = None
                active = any(slot is not None for slot in self.slots)
            if active:
                self._render_locked()
            else:
                if Progress.rows > 0:
                    sys.stdout.write(f"\033[{max(0, Progress.rows - 1)}A\033[J")
                    Progress.rows = 0
                sys.stdout.flush()

    def set_waiting(self, order: int, deadline: float) -> None:
        """标记章节进入获取重试等待，进度区显示实时倒计时（每秒重绘）。"""
        with _io_lock:
            with self.lock:
                self.waiting[order] = deadline
                self.last_activity[order] = time.time()
            self._render_locked()

    def clear_waiting(self, order: int) -> None:
        with _io_lock:
            with self.lock:
                self.waiting.pop(order, None)
                self.last_activity.pop(order, None)
            self._render_locked()

    def _render_locked(self) -> None:
        """渲染进度区；调用方必须已持有 _io_lock 和 self.lock。"""
        if self.menu_open:
            return
        done = self.done_chapters
        total = self.total_chapters
        overall = done * 100.0 / total
        lines: list[str] = []

        def page_number(slot: dict[str, Any]) -> int:
            m = re.search(r"(\d+)", str(slot["filename"]))
            return int(m.group(1)) if m else 0

        groups: dict[int, list[dict[str, Any]]] = {}
        for slot in self.slots:
            if slot is None:
                continue
            groups.setdefault(int(slot["order"]), []).append(slot)

        # 按章节号顺序排列，下载中与等待重试的章节各占一行
        for index, order in enumerate(sorted(set(list(groups) + list(self.waiting)))):
            if index >= self.slot_count:
                break
            waiting_deadline = self.waiting.get(order)
            if waiting_deadline is not None:
                remaining = max(0, int(waiting_deadline - time.time()))
                minutes, seconds = divmod(remaining, 60)
                line = f"  第{order}话 获取失败，重试倒计时 {minutes:02d}:{seconds:02d}"
            else:
                slots = groups[order]
                current = min(slots, key=page_number)
                filename = str(current["filename"])
                pct = float(current["percent"])
                if pct > 100:
                    pct = 100
                bar_len = 20
                filled = int(bar_len * pct / 100)
                bar = "█" * filled + "░" * (bar_len - filled)
                line = f"  第{order}话 {filename} [{bar}] {pct:5.1f}%"
            if index == 0:
                suffix = f"总进度 {overall:.0f}%"
                if waiting_deadline is None:
                    last = self.last_activity.get(order, time.time())
                    remaining = self.stall_timeout - (time.time() - last)
                    if remaining < 0:
                        remaining = 0
                    minutes, seconds = divmod(int(remaining), 60)
                    suffix += f"  换源倒计时 {minutes:02d}:{seconds:02d}"
                line += f"  {suffix}"
            lines.append(line)
        while len(lines) < self.slot_count:
            lines.append("  等待中...")
        if Progress.rows > 0:
            sys.stdout.write(f"\033[{max(0, Progress.rows - 1)}A\033[J")
        for index, line in enumerate(lines):
            sys.stdout.write(line)
            if index < len(lines) - 1:
                sys.stdout.write("\n")
        Progress.rows = self.slot_count
        sys.stdout.flush()


# ---------------------------------------------------------------- 下载与校验


IMAGE_EXTS = {"jpg", "jpeg", "png", "webp", "gif", "avif", "bmp"}


def image_extension(image_url: str, fallback: str = ".jpg") -> str:
    try:
        parsed = requests.utils.urlparse(image_url)
        segment = parsed.path.rstrip("/").split("/")[-1]
        name = requests.utils.unquote(segment)
        match = re.search(r"\.([A-Za-z0-9]+)$", name)
        if match and match.group(1).lower() in IMAGE_EXTS:
            return "." + match.group(1).lower()
    except Exception:
        pass
    return fallback


def detect_image_signature(path: Path) -> bool:
    try:
        size = path.stat().st_size
        if size <= 0:
            return False
        with open(path, "rb") as fh:
            head = fh.read(16)
        if head[:3] == b"\xff\xd8\xff":
            return True
        if head[:8] == b"\x89PNG\r\n\x1a\n":
            return True
        if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
            return True
        if head[:6] in (b"GIF87a", b"GIF89a"):
            return True
        if head[4:8] == b"ftyp" and head[8:12] in (b"avif", b"avis", b"heic", b"mif1"):
            return True
        if head[:2] == b"BM":
            return True
        return True
    except OSError:
        return False


class ImageDownloader:
    def __init__(
        self,
        workers: int = 4,
        rate_per_minute: int = 20,
        timeout: float = 60.0,
        retries: int = 3,
    ):
        self.workers = max(1, workers)
        self.rate_per_minute = rate_per_minute
        self.timeout = timeout
        self.retries = retries
        self._local = threading.local()
        self.limiter = RateLimiter(rate_per_minute)

    def _session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            self._local.session = session
        return session

    def _fetch(
        self,
        url: str,
        headers: dict[str, str],
        progress: Progress | None = None,
        slot_index: int = -1,
    ) -> bytes | None:
        last_error: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                self.limiter.acquire()
                resp = self._session().get(
                    url, headers=headers, timeout=self.timeout, stream=True
                )
                resp.raise_for_status()
                total = int(resp.headers.get("Content-Length") or 0)
                chunks: list[bytes] = []
                downloaded = 0
                for chunk in resp.iter_content(chunk_size=65536):
                    if not chunk:
                        continue
                    chunks.append(chunk)
                    downloaded += len(chunk)
                    if progress is not None and slot_index >= 0 and total:
                        progress.update_slot(slot_index, downloaded * 100.0 / total)
                body = b"".join(chunks)
                if not body:
                    raise SourceError("empty image body")
                if progress is not None and slot_index >= 0:
                    progress.update_slot(slot_index, 100.0)
                return body
            except (requests.RequestException, SourceError) as exc:
                last_error = exc
                if attempt < self.retries:
                    time.sleep(min(2 ** attempt, 8))
        emit(c_warn(f"    [warn] 图片下载失败: {url} ({last_error})"))
        return None

    def download_cover(self, source: SourceAdapter, comic: ComicInfo, comic_dir: Path) -> bool:
        url = (comic.cover or "").strip()
        if not url:
            return False
        ext = image_extension(url)
        target = comic_dir / f"封面{ext}"
        if target.exists() and target.stat().st_size > 0 and detect_image_signature(target):
            return True
        headers = dict(source.image_headers(url))
        body = self._fetch(url, headers)
        if not body:
            return False
        tmp = target.with_suffix(target.suffix + ".part")
        tmp.write_bytes(body)
        tmp.replace(target)
        return True

    def download_chapter(
        self,
        source: SourceAdapter,
        comic: ComicInfo,
        chapter,
        urls: list[str],
        chapter_dir: Path,
        flat: bool = False,
        progress: Progress | None = None,
        cancel_event: threading.Event | None = None,
    ) -> tuple[bool, list[int], int]:
        if not urls:
            return False, [], 0

        force_ext = source.force_ext()
        tasks: list[tuple[int, str, Path]] = []
        for index, url in enumerate(urls):
            ext = force_ext or image_extension(url)
            if flat:
                filename = f"{chapter_folder_name(chapter.order)}_{index + 1:03d}{ext}"
            else:
                filename = f"{index + 1:03d}{ext}"
            target = chapter_dir / filename
            normalize_page_file(
                chapter_dir, index + 1, target,
                order=chapter.order, flat=flat,
            )
            tasks.append((index + 1, url, target))

        existing = sum(
            1 for _, _, path in tasks if path.exists() and path.stat().st_size > 0
        )
        failed_pages: list[int] = []
        headers_template = source.image_headers("")

        def run_task(task: tuple[int, str, Path]) -> tuple[int, bool]:
            page_no, url, path = task
            slot_index = -1
            try:
                if progress is not None:
                    slot_index = progress.acquire_slot(chapter.order, path.name)
                if cancel_event is not None and cancel_event.is_set():
                    return page_no, False
                if path.exists() and path.stat().st_size > 0:
                    return page_no, True
                headers = dict(headers_template)
                headers.update(source.image_headers(url))
                body = self._fetch(url, headers, progress=progress, slot_index=slot_index)
                if body is None:
                    return page_no, False
                try:
                    body = source.decrypt_image(body)
                except Exception:
                    return page_no, False
                if not body:
                    return page_no, False
                tmp = path.with_suffix(path.suffix + ".part")
                tmp.write_bytes(body)
                tmp.replace(path)
                return page_no, True
            finally:
                if progress is not None and slot_index >= 0:
                    progress.release_slot(slot_index)

        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = {pool.submit(run_task, task): task[0] for task in tasks}
            for future in as_completed(futures):
                page_no = futures[future]
                try:
                    ok = future.result()
                except Exception:
                    ok = False
                if not ok:
                    failed_pages.append(page_no)

        for page_no, _, path in tasks:
            if page_no not in failed_pages and not detect_image_signature(path):
                failed_pages.append(page_no)

        if failed_pages:
            for page_no, _, path in tasks:
                if page_no in failed_pages:
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        pass
            return False, failed_pages, existing

        return True, [], existing


# ---------------------------------------------------------------- 章节目录


def find_chapter_dir(comic_dir: Path, order: int) -> Path | None:
    target = comic_dir / chapter_folder_name(order)
    return target if target.is_dir() else None


def prepare_chapter_dir(
    comic_dir: Path, order: int, flat: bool = False, reset: bool = False
) -> Path:
    if flat:
        return comic_dir
    target = comic_dir / chapter_folder_name(order)
    if reset and target.is_dir() and not (target / ".done").exists():
        for item in target.iterdir():
            try:
                if item.is_file():
                    item.unlink()
            except OSError:
                pass
    target.mkdir(parents=True, exist_ok=True)
    return target


# ---------------------------------------------------------------- 记录器


class ResultRecorder:
    def __init__(self, log_path: Path, jsonl_path: Path, fail_path: Path):
        self.log_path = log_path
        self.jsonl_path = jsonl_path
        self.fail_path = fail_path
        self._lock = threading.Lock()

    def record(
        self,
        name: str,
        status: str,
        message: str = "",
        matched_title: str = "",
        comic_id: str = "",
        source_label: str = "",
        total_chapters: int = 0,
        ok_chapters: int = 0,
        failed_chapters: list[str] | None = None,
    ) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        record = {
            "time": now,
            "name": name,
            "status": status,
            "source": source_label,
            "matched_title": matched_title,
            "comic_id": comic_id,
            "total_chapters": total_chapters,
            "ok_chapters": ok_chapters,
            "failed_chapters": failed_chapters or [],
            "message": message,
        }
        line = f"[{now}] {name} => {status}"
        if source_label:
            line += f" [{source_label}]"
        if matched_title:
            line += f" ({matched_title})"
        if message:
            line += f": {message}"
        with self._lock:
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            with open(self.jsonl_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            if status in ("failed", "no_result") or failed_chapters:
                fail_line = line
                if failed_chapters and "失败章节" not in line:
                    fail_line += f": 失败章节: {'; '.join(failed_chapters)}"
                with open(self.fail_path, "a", encoding="utf-8") as fh:
                    fh.write(fail_line + "\n")
        if status == "success":
            emit(c_ok(line))
        elif status in ("failed", "no_result"):
            emit(c_fail(line))
        else:
            emit(c_info(line))


# ---------------------------------------------------------------- 聚合搜索

SOURCE_PRIORITY: dict[str, int] = {
    "copy": 0,
    "baozimh": 1,
    "zmh": 2,
    "rumanhua": 3,
    "manwa3": 4,
    "manhuagui": 5,
}


def readable_chapter_count(comic: ComicInfo) -> int:
    return sum(1 for chapter in comic.chapters if chapter.extra.get("can_read") is not False)


def aggregate_candidates(
    sources: list[SourceAdapter],
    keyword: str,
    api_limiter: RateLimiter,
    threshold: float = 0.55,
    api_interval: float = 1.0,
    source_priority: list[str] | None = None,
    chapter_gap_threshold: int = 15,
) -> list[tuple[SourceAdapter, ComicInfo, float]]:
    matches: list[tuple[SourceAdapter, ComicInfo, float]] = []
    lock = threading.Lock()

    def try_source(source: SourceAdapter) -> None:
        try:
            api_limiter.acquire()
            time.sleep(api_interval)
            results = source.search(keyword)
            if not results:
                return
            matched = find_best_match(results, keyword, threshold)
            if matched is None:
                return
            matched_result, score = matched
            api_limiter.acquire()
            time.sleep(api_interval)
            comic = source.get_comic(matched_result)
            if not comic.chapters:
                return
            readable = readable_chapter_count(comic)
            with lock:
                matches.append((source, comic, score))
            emit(
                c_info(f"  源[{source.label}] 匹配: {comic.title} "
                f"(id={comic.id}, 章节={len(comic.chapters)}, 可读={readable}, 匹配={score:.2f})"
                )
            )
        except Exception as exc:
            emit(c_warn(f"  源[{source.label}] 不可用: {exc}"))

    with ThreadPoolExecutor(max_workers=len(sources)) as pool:
        list(pool.map(try_source, sources))

    priority_map: dict[str, int] = {}
    if source_priority:
        priority_map = {name: index for index, name in enumerate(source_priority)}
    default_priority = len(priority_map) + 1

    matches.sort(
        key=lambda item: (
            priority_map.get(item[0].name, default_priority),
            -readable_chapter_count(item[1]),
            -len(item[1].chapters),
            -item[2],
        ),
    )

    # 第一优先源与章节最多的源差距过大时，判定第一优先匹配到同人/错误作品，改选章节最多的源
    if chapter_gap_threshold > 0 and len(matches) >= 2:
        max_chapters = max(len(item[1].chapters) for item in matches)
        if max_chapters - len(matches[0][1].chapters) >= chapter_gap_threshold:
            emit(
                c_warn(
                    f"  章节差距 {max_chapters - len(matches[0][1].chapters)} 章 >= {chapter_gap_threshold}，"
                    "疑似匹配到同人，自动选择章节最多的源"
                )
            )
            matches.sort(
                key=lambda item: (
                    -len(item[1].chapters),
                    -readable_chapter_count(item[1]),
                    priority_map.get(item[0].name, default_priority),
                    -item[2],
                ),
            )
    return matches


# ---------------------------------------------------------------- 下载流程


def download_comic(
    source: SourceAdapter,
    comic: ComicInfo,
    comic_dir: Path,
    downloader: ImageDownloader,
    api_limiter: RateLimiter,
    chapter_range: tuple[int, int] | None = None,
    flat: bool = False,
    resume: bool = True,
    chapter_workers: int = 2,
    refresh: bool = False,
    cache_ttl: float = 86400,
    api_interval: float = 1.0,
    stall_timeout: int = 300,
    fetch_retry_seconds: float = 65.0,
    ctrl: ControlState | None = None,
) -> tuple[int, list[str]]:
    if ctrl is None:
        ctrl = ControlState()
    selected = [
        chapter
        for chapter in comic.chapters
        if not chapter_range or chapter_range[0] <= chapter.order <= chapter_range[1]
    ]
    global _active_progress
    progress = Progress(len(selected), slot_count=4, stall_timeout=stall_timeout)
    _active_progress = progress
    check_lock = threading.Lock()
    cancel_events: dict[int, threading.Event] = {}
    ok_count = 0
    failed: list[str] = []
    fail_lock = threading.Lock()
    last_ok_order: int | None = None

    def work(chapter) -> tuple[str, str]:
        label = chapter_folder_name(chapter.order)
        ev = cancel_events[chapter.order]
        if ev.is_set():
            return "skip", label
        chapter_dir = prepare_chapter_dir(
            comic_dir, chapter.order, flat=flat, reset=not resume
        )
        emit(c_info(f"  [{chapter.order:03d}] 获取 {chapter.name} 图片列表..."))
        urls = None
        if not refresh:
            urls = read_chapter_cache(
                chapter_dir,
                chapter.order,
                source.name,
                flat=flat,
                ttl_seconds=cache_ttl,
            )
        if urls is None:
            deadline = time.monotonic() + max(0.0, fetch_retry_seconds)
            attempt = 0
            while True:
                attempt += 1
                try:
                    with check_lock:
                        api_limiter.acquire()
                        time.sleep(api_interval)
                        urls = source.get_chapter_images(comic, chapter)
                    if urls:
                        write_chapter_cache(
                            chapter_dir, chapter.order, source.name, urls, flat=flat
                        )
                    break
                except Exception as exc:
                    remaining = deadline - time.monotonic()
                    if ev.is_set() or ctrl.exit_requested or remaining <= 0:
                        progress.mark_done(chapter.order)
                        emit(c_fail(
                            f"    [fail] 获取章节失败（重试 {fetch_retry_seconds:.0f}s 后放弃）: {exc}"
                        ))
                        return "fail", f"{label} (获取失败: {exc})"
                    wait = min(5.0, max(1.0, remaining))
                    emit(c_warn(
                        f"    [retry] 获取章节失败（第 {attempt} 次）: {exc}，"
                        f"{wait:.0f}s 后重试（剩余 {remaining:.0f}s）"
                    ))
                    progress.set_waiting(chapter.order, deadline)
                    slept = 0.0
                    try:
                        while (
                            slept < wait
                            and not ev.is_set()
                            and not ctrl.exit_requested
                        ):
                            time.sleep(1.0)
                            slept += 1.0
                            progress.set_waiting(chapter.order, deadline)
                    finally:
                        progress.clear_waiting(chapter.order)
        else:
            emit(c_info(f"  [{chapter.order:03d}] 使用缓存校验 ({len(urls)} 页)"))
        if not urls:
            progress.mark_done(chapter.order)
            emit(c_fail("    [fail] 章节没有可用图片"))
            return "fail", f"{label} (无图片)"
        existing_pages = count_valid_pages(
            chapter_dir, len(urls), order=chapter.order, flat=flat
        )
        if existing_pages >= len(urls):
            force_ext = source.force_ext()
            for index, url in enumerate(urls):
                ext = force_ext or image_extension(url)
                if flat:
                    target = chapter_dir / f"{chapter_folder_name(chapter.order)}_{index + 1:03d}{ext}"
                else:
                    target = chapter_dir / f"{index + 1:03d}{ext}"
                normalize_page_file(
                    chapter_dir, index + 1, target,
                    order=chapter.order, flat=flat,
                )
            progress.mark_done(chapter.order)
            emit(c_ok(f"    [ok] 页数完整 ({existing_pages}/{len(urls)})，跳过"))
            return "ok", label
        progress.set_chapter_total(chapter.order, len(urls))
        emit(c_warn(f"    下载 {len(urls)} 张图片（已有 {existing_pages}）..."))
        success, failed_pages, existing = downloader.download_chapter(
            source,
            comic,
            chapter,
            urls,
            chapter_dir,
            flat=flat,
            progress=progress,
            cancel_event=ev,
        )
        progress.finish_chapter(chapter.order)
        if ev.is_set():
            emit(c_warn(f"  [skip] {label} 已跳过"))
            return "skip", label
        if success:
            emit(c_ok(f"    [ok] {len(urls)} 页完成（已存在 {existing}）"))
            return "ok", label
        emit(c_fail(f"    [fail] 页码 {failed_pages} 下载失败"))
        return "fail", f"{label} (页码失败: {failed_pages})"

    def open_menu() -> None:
        progress.menu_open = True
        menu_lines = 9
        with _io_lock:
            if Progress.rows > 0:
                sys.stdout.write(f"\033[{max(0, Progress.rows - 1)}A\033[J")
                Progress.rows = 0
            sys.stdout.write("\n=== 控制菜单 ===\n")
            sys.stdout.write("1. 跳过当前漫画\n")
            sys.stdout.write("2. 跳过当前章节\n")
            sys.stdout.write("3. 重新下载上一章节\n")
            sys.stdout.write("4. 暂停 / 继续\n")
            sys.stdout.write("5. 显示当前状态\n")
            sys.stdout.write("6. 退出程序\n")
            sys.stdout.write("Esc 关闭菜单\n")
            sys.stdout.write("请选择: ")
            sys.stdout.flush()
        try:
            while True:
                ch = msvcrt.getch()
                if ch == b"1":
                    ctrl.skip_comic = True
                    break
                if ch == b"2":
                    ctrl.skip_chapter = True
                    break
                if ch == b"3":
                    ctrl.redo_prev = True
                    break
                if ch == b"4":
                    ctrl.paused = not ctrl.paused
                    break
                if ch == b"5":
                    status = f"已完成 {progress.done_chapters}/{progress.total_chapters} 章"
                    if last_ok_order is not None:
                        status += f"，上一成功章节 第{last_ok_order}话"
                    with _io_lock:
                        sys.stdout.write("\n" + status + "\n")
                        sys.stdout.flush()
                    continue
                if ch == b"6":
                    ctrl.exit_requested = True
                    break
                if ch in (b"\x1b", b"\x00"):
                    break
        except KeyboardInterrupt:
            ctrl.exit_requested = True
        finally:
            progress.menu_open = False
            with _io_lock:
                sys.stdout.write(f"\033[{menu_lines - 1}A\033[J")
                Progress.rows = 0
                sys.stdout.flush()
            if not ctrl.exit_requested:
                with _io_lock:
                    progress._render_locked()

    def poll_keys() -> None:
        if not HAS_MSVCRT:
            return
        try:
            while msvcrt.kbhit():
                ch = msvcrt.getch()
                if ch == b"\x06":  # Ctrl+F
                    open_menu()
                    return
        except Exception:
            pass

    queue = deque(selected)
    active: dict[Any, tuple] = {}
    pool = ThreadPoolExecutor(max_workers=max(1, chapter_workers))
    try:
        while (queue or active) and not ctrl.exit_requested:
            if ctrl.skip_comic:
                ctrl.skip_comic = False
                queue.clear()
                for _, ev in active.values():
                    ev.set()
            if ctrl.skip_chapter:
                ctrl.skip_chapter = False
                for _, ev in active.values():
                    ev.set()
            if ctrl.redo_prev and last_ok_order is not None:
                ctrl.redo_prev = False
                prev = next(
                    (c for c in comic.chapters if c.order == last_ok_order), None
                )
                if prev is not None:
                    reset_chapter_dir(comic_dir, prev.order, flat=flat)
                    queued_orders = [c.order for c in queue]
                    queued_orders += [c.order for c, _ in active.values()]
                    if prev.order not in queued_orders:
                        queue.appendleft(prev)
                        ok_count = max(0, ok_count - 1)
            while (
                not ctrl.paused
                and not ctrl.exit_requested
                and queue
                and len(active) < max(1, chapter_workers)
            ):
                chapter = queue.popleft()
                ev = threading.Event()
                cancel_events[chapter.order] = ev
                fut = pool.submit(work, chapter)
                active[fut] = (chapter, ev)
            now = time.time()
            for fut, (chapter, ev) in list(active.items()):
                last = progress.last_activity.get(chapter.order, now)
                if now - last > stall_timeout:
                    ev.set()
                    with fail_lock:
                        failed.append(f"第{chapter.order}话 (下载超时，已触发换源)")
                    emit(c_fail(f"  [fail] 第{chapter.order}话 下载超时，触发换源"))
            done: set[Any] = set()
            if active:
                done, _ = wait(
                    set(active), timeout=0.5, return_when=FIRST_COMPLETED
                )
            else:
                time.sleep(0.2)
            for fut in done:
                chapter, ev = active.pop(fut)
                try:
                    status, detail = fut.result()
                except Exception as exc:
                    status, detail = "fail", f"第{chapter.order}话 (异常: {exc})"
                if status == "ok":
                    ok_count += 1
                    last_ok_order = chapter.order
                elif status == "fail":
                    with fail_lock:
                        failed.append(detail)
            if not ctrl.exit_requested:
                poll_keys()
    finally:
        for _, ev in active.values():
            ev.set()
        pool.shutdown(wait=False, cancel_futures=True)
        _active_progress = None
    return ok_count, failed


# ---------------------------------------------------------------- 主流程


def read_name_list(path: Path) -> list[str]:
    if not path.exists():
        raise FileNotFoundError(f"名单文件不存在: {path}")
    names: list[str] = []
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if " # " in line:
            line = line.split(" # ", 1)[0].strip()
        if line:
            names.append(line)
    return names


def process_names(
    names: list[str],
    sources: list[SourceAdapter],
    downloader: ImageDownloader,
    api_limiter: RateLimiter,
    out_dir: Path,
    recorder: ResultRecorder,
    match_threshold: float = 0.55,
    chapter_range: tuple[int, int] | None = None,
    flat: bool = False,
    resume: bool = True,
    chapter_workers: int = 2,
    fallback: bool = True,
    refresh: bool = False,
    cache_ttl: float = 86400,
    api_interval: float = 1.0,
    stall_timeout: int = 300,
    fetch_retry_seconds: float = 65.0,
    ctrl: ControlState | None = None,
    source_priority: list[str] | None = None,
    chapter_gap_threshold: int = 15,
) -> None:
    if ctrl is None:
        ctrl = ControlState()
    for name in names:
        if ctrl.exit_requested:
            break
        emit(c_head(f"\n=== 处理: {name} ==="))
        comic_dir = out_dir / sanitize_filename(name, name)
        comic_dir.mkdir(parents=True, exist_ok=True)
        candidates = aggregate_candidates(
            sources,
            name,
            api_limiter,
            match_threshold,
            api_interval,
            source_priority,
            chapter_gap_threshold,
        )
        if not candidates:
            # 无匹配结果时在 fetch_retry_seconds 内持续重试，超时仍无结果才跳过
            deadline = time.monotonic() + max(0.0, fetch_retry_seconds)
            attempt = 0
            while not candidates:
                attempt += 1
                remaining = deadline - time.monotonic()
                if remaining <= 0 or ctrl.exit_requested:
                    break
                wait = min(5.0, max(1.0, remaining))
                emit(c_warn(
                    f"  [retry] 无匹配结果（第 {attempt} 次）: {name}，"
                    f"{wait:.0f}s 后重试（剩余 {remaining:.0f}s）"
                ))
                slept = 0.0
                while slept < wait and not ctrl.exit_requested:
                    time.sleep(0.2)
                    slept += 0.2
                candidates = aggregate_candidates(
                    sources,
                    name,
                    api_limiter,
                    match_threshold,
                    api_interval,
                    source_priority,
                    chapter_gap_threshold,
                )
        if not candidates:
            recorder.record(
                name,
                "no_result",
                message=(
                    f"所有资源均无匹配结果（阈值 {match_threshold:.2f}，"
                    f"重试 {fetch_retry_seconds:.0f}s 后放弃）"
                ),
            )
            continue

        used_sources: list[str] = []
        final_comic = candidates[0][1]
        final_failed: list[str] = []
        all_failed: list[str] = []
        cover_ok = False

        for index, (source, comic, score) in enumerate(candidates):
            used_sources.append(source.label)
            final_comic = comic
            emit(c_info(f"  尝试源[{source.label}] 下载... (匹配 {score:.2f})"))
            if not cover_ok:
                try:
                    if downloader.download_cover(source, comic, comic_dir):
                        cover_ok = True
                        emit(c_ok("  封面已就绪"))
                    else:
                        emit(c_warn("  [warn] 封面下载失败或无封面地址"))
                except Exception as exc:
                    emit(c_warn(f"  [warn] 封面下载异常: {exc}"))
            try:
                ok_chapters, failed_chapters = download_comic(
                    source,
                    comic,
                    comic_dir,
                    downloader,
                    api_limiter,
                    chapter_range=chapter_range,
                    flat=flat,
                    resume=resume,
                    chapter_workers=chapter_workers,
                    refresh=refresh,
                    cache_ttl=cache_ttl,
                    api_interval=api_interval,
                    stall_timeout=stall_timeout,
                    fetch_retry_seconds=fetch_retry_seconds,
                    ctrl=ctrl,
                )
            except Exception as exc:
                ok_chapters = 0
                failed_chapters = [f"下载过程异常: {exc}"]
                emit(c_fail(f"  源[{source.label}] 下载异常: {exc}"))

            final_failed = failed_chapters
            if failed_chapters:
                all_failed.extend(failed_chapters)
            if not failed_chapters or ctrl.exit_requested:
                break
            if fallback and index < len(candidates) - 1:
                emit(c_warn(f"  源[{source.label}] 有失败章节，切换下一源继续剩余章节..."))

        if not cover_ok:
            for source, comic, _score in candidates:
                try:
                    if downloader.download_cover(source, comic, comic_dir):
                        cover_ok = True
                        emit(c_ok(f"  封面已由源[{source.label}]补齐"))
                        break
                except Exception:
                    continue

        selected_total = sum(
            1
            for chapter in final_comic.chapters
            if not chapter_range or chapter_range[0] <= chapter.order <= chapter_range[1]
        )
        ok_chapters = selected_total - len(final_failed)
        status = "success" if not final_failed else "failed"
        recorder.record(
            name,
            status,
            message="" if not all_failed else f"失败章节: {'; '.join(all_failed)}",
            matched_title=final_comic.title,
            comic_id=final_comic.id,
            source_label=" -> ".join(used_sources),
            total_chapters=selected_total,
            ok_chapters=ok_chapters,
            failed_chapters=all_failed,
        )


# ---------------------------------------------------------------- CLI


def parse_range(text: str | None) -> tuple[int, int] | None:
    if not text or not text.strip():
        return None
    parts = text.strip().split("-")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError(f"章节范围格式应为 开始-结束: {text}")
    start, end = int(parts[0]), int(parts[1])
    if start < 1 or end < start:
        raise argparse.ArgumentTypeError(f"非法章节范围: {text}")
    return start, end


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="多源漫画批量下载器：名单 -> 聚合搜索 -> 选资源 -> 下载 -> 校验 -> 记录",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--list", type=Path, default=Path("名单.txt"), help="名单文件，每行一个名称")
    parser.add_argument("--out", type=Path, default=Path("downloads"), help="下载输出目录")
    parser.add_argument(
        "--sources",
        default="copy,zmh,baozimh,rumanhua",
        help="启用资源，逗号分隔",
    )
    parser.add_argument("--source-priority", default="", help="下载源优先级，逗号分隔，靠前优先")
    parser.add_argument("--chapter-gap-threshold", type=int, default=15, help="源间章节数差距达到该值自动选章节最多的源（0 关闭）")
    parser.add_argument("--match-threshold", type=float, default=0.55, help="名称近似匹配阈值（0-1）")
    parser.add_argument("--api", default="hot2", help="拷贝漫画 API 线路")
    parser.add_argument("--api-base", default="", help="拷贝漫画 API 基础 URL（优先于 --api）")
    parser.add_argument("--platform", default="1", help="拷贝漫画 platform 参数")
    parser.add_argument("--zmh-account", default="", help="再漫画登录账号")
    parser.add_argument("--zmh-password", default="", help="再漫画登录密码")
    parser.add_argument("--range", type=parse_range, default=None, metavar="开始-结束", help="只下载指定章节范围")
    parser.add_argument("--chapter-workers", type=int, default=1, help="同时下载的章节数")
    parser.add_argument("--workers", type=int, default=4, help="每章图片并发线程数")
    parser.add_argument("--rate", type=int, default=20, help="图片每分钟请求上限（0 为不限制）")
    parser.add_argument("--api-rate", type=int, default=0, help="API 每分钟请求上限（0 为不限制）")
    parser.add_argument("--api-interval", type=float, default=0.0, help="每次源检查请求之间的间隔秒数（0 为不间隔）")
    parser.add_argument("--flat", action="store_true", help="所有章节图片平铺到漫画目录")
    parser.add_argument("--no-fallback", action="store_true", help="禁用换源续传")
    parser.add_argument("--fail", type=Path, default=Path("失败.log"), help="失败结果日志文件")
    parser.add_argument("--log", type=Path, default=Path("result.log"), help="文本日志文件")
    parser.add_argument("--jsonl", type=Path, default=Path("result.jsonl"), help="结构化结果日志文件")
    parser.add_argument("--timeout", type=float, default=60.0, help="图片请求超时秒数")
    parser.add_argument("--api-timeout", type=float, default=30.0, help="API 请求超时秒数")
    parser.add_argument("--stall-timeout", type=int, default=300, help="章节卡住多少秒后换源")
    parser.add_argument("--fetch-retry", type=float, default=65.0, help="章节图片列表获取失败后重试的总时长秒数，期间持续重试，超时仍失败才跳过该章")
    parser.add_argument("--no-resume", action="store_true", help="禁用断点续传")
    parser.add_argument("--cache-ttl", type=float, default=24, help="章节缓存有效期（小时）")
    parser.add_argument("--refresh", action="store_true", help="忽略缓存，强制重新获取章节图片列表")
    return parser


def load_config_file(path: Path) -> dict[str, Any]:
    """读取 config.json，支持每行 # 结尾注释（标准 JSON 不允许注释）。"""
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8-sig")
    lines: list[str] = []
    for raw_line in text.splitlines():
        out: list[str] = []
        in_string = False
        escape = False
        for ch in raw_line:
            if escape:
                out.append(ch)
                escape = False
            elif ch == "\\" and in_string:
                out.append(ch)
                escape = True
            elif ch == '"':
                in_string = not in_string
                out.append(ch)
            elif ch == "#" and not in_string:
                break
            else:
                out.append(ch)
        lines.append("".join(out))
    cleaned = "\n".join(lines)
    try:
        data = json.loads(cleaned)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def apply_config(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
    config: dict[str, Any],
) -> argparse.Namespace:
    """用 config.json 填充未被命令行显式指定的参数（命令行优先级更高）。"""
    string_keys = {
        "list_file": "list",
        "out_dir": "out",
        "sources": "sources",
        "copy_api": "api",
        "copy_api_base": "api_base",
        "copy_platform": "platform",
        "zmh_account": "zmh_account",
        "zmh_password": "zmh_password",
        "fail_log": "fail",
        "log_file": "log",
        "jsonl_file": "jsonl",
    }
    int_keys = {
        "chapter_workers": "chapter_workers",
        "image_workers": "workers",
        "image_rate": "rate",
        "api_rate": "api_rate",
        "stall_timeout": "stall_timeout",
        "cache_ttl_hours": "cache_ttl",
        "chapter_gap_threshold": "chapter_gap_threshold",
    }
    float_keys = {
        "match_threshold": "match_threshold",
        "api_interval": "api_interval",
        "timeout": "timeout",
        "api_timeout": "api_timeout",
        "fetch_retry_seconds": "fetch_retry",
    }

    path_keys = {"list_file", "out_dir", "fail_log", "log_file", "jsonl_file"}
    for key, attr in string_keys.items():
        if key in config and getattr(args, attr) == parser.get_default(attr):
            value: Any = str(config[key])
            if key in path_keys:
                value = Path(value)
            setattr(args, attr, value)
    for key, attr in int_keys.items():
        if key in config and getattr(args, attr) == parser.get_default(attr):
            try:
                setattr(args, attr, int(config[key]))
            except (TypeError, ValueError):
                pass
    for key, attr in float_keys.items():
        if key in config and getattr(args, attr) == parser.get_default(attr):
            try:
                setattr(args, attr, float(config[key]))
            except (TypeError, ValueError):
                pass

    if config.get("flat") and not args.flat:
        args.flat = True
    if config.get("refresh") and not args.refresh:
        args.refresh = True
    if config.get("fallback") is False and not args.no_fallback:
        args.no_fallback = True
    if config.get("resume") is False and not args.no_resume:
        args.no_resume = True

    if "source_priority" in config and not args.source_priority:
        args.source_priority = str(config["source_priority"])

    if "chapter_range" in config and config["chapter_range"] and args.range is None:
        try:
            args.range = parse_range(str(config["chapter_range"]))
        except argparse.ArgumentTypeError:
            pass
    return args


def main() -> int:
    ctrl = ControlState()
    install_sigint(ctrl)
    try:
        return _main_inner(ctrl)
    except KeyboardInterrupt:
        emit(c_warn("\n用户退出。"))
        return 130


def _main_inner(ctrl: ControlState) -> int:
    args = build_parser().parse_args()
    config_data = load_config_file(Path("config.json"))
    args = apply_config(args, build_parser(), config_data)

    names = read_name_list(args.list)
    if not names:
        emit(f"名单为空: {args.list}")
        return 1
    emit(c_info(f"读取名单 {len(names)} 项: {args.list}"))

    enabled_sources = [s.strip().lower() for s in args.sources.split(",") if s.strip()]
    zmh_account = str(args.zmh_account or "").strip()
    zmh_password = args.zmh_password or ""
    sources = build_sources(
        enabled=enabled_sources,
        copy_api_choice=args.api,
        copy_api_base=args.api_base,
        platform=args.platform,
        timeout=args.api_timeout,
        zmh_account=zmh_account,
        zmh_password=zmh_password,
    )
    emit(c_info(f"启用资源: {', '.join(source.label for source in sources)}"))
    if "zmh" in enabled_sources and zmh_account:
        for source in sources:
            if source.name == "zmh":
                if source.ensure_login():
                    emit(c_ok("再漫画已自动登录"))
                else:
                    emit(c_warn("再漫画自动登录失败，将跳过需要权限的章节"))
                break

    downloader = ImageDownloader(
        workers=args.workers,
        rate_per_minute=max(0, args.rate),
        timeout=args.timeout,
    )
    api_limiter = RateLimiter(max(0, args.api_rate))
    recorder = ResultRecorder(
        log_path=args.log,
        jsonl_path=args.jsonl,
        fail_path=args.fail,
    )

    out_dir = args.out
    out_dir.mkdir(parents=True, exist_ok=True)

    process_names(
        names,
        sources,
        downloader,
        api_limiter,
        out_dir,
        recorder,
        match_threshold=args.match_threshold,
        chapter_range=args.range,
        flat=args.flat,
        resume=not args.no_resume,
        chapter_workers=args.chapter_workers,
        fallback=not args.no_fallback,
        refresh=args.refresh,
        cache_ttl=args.cache_ttl * 3600,
        api_interval=args.api_interval,
        stall_timeout=args.stall_timeout,
        fetch_retry_seconds=args.fetch_retry,
        ctrl=ctrl,
        source_priority=(
            [s.strip().lower() for s in args.source_priority.split(",") if s.strip()]
            if args.source_priority
            else None
        ),
        chapter_gap_threshold=args.chapter_gap_threshold,
    )

    emit(c_info("\n全部处理完成。"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
    for name in names:
        emit(c_head(f"\n=== 处理: {name} ==="))
        comic_dir = out_dir / sanitize_filename(name, name)
        comic_dir.mkdir(parents=True, exist_ok=True)
