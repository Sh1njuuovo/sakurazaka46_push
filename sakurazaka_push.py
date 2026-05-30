import hashlib
import json
import logging
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup, Tag
from dotenv import load_dotenv


BASE_URL = "https://sakurazaka46.com"
NEWS_URL = "https://sakurazaka46.com/s/s46/news/list?ima=0000"
BLOG_URL = "https://sakurazaka46.com/s/s46/diary/blog?ima=0000"
BLOG_FALLBACK_URL = "https://sakurazaka46.com/s/s46/diary/blog/list?ima=0000"
SCHEDULE_URL = "https://sakurazaka46.com/s/s46/media/list?ima=0000"
PUSHPLUS_URL = "https://www.pushplus.plus/send"
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


@dataclass(frozen=True)
class Item:
    section: str
    date: str
    meta: str
    title: str
    url: str


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        logging.warning("%s=%r 不是有效整数，使用默认值 %s。", name, value, default)
        return default


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def normalize_url(url: str, base: str = BASE_URL) -> str:
    absolute = urljoin(base, url)
    parsed = urlparse(absolute)
    if parsed.netloc and parsed.netloc != "sakurazaka46.com":
        return absolute
    query = urlencode(
        [(key, value) for key, value in parse_qsl(parsed.query) if key.lower() not in {"ima"}],
        doseq=True,
    )
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, query, ""))


def is_official_url(url: str) -> bool:
    return urlparse(url).netloc in {"", "sakurazaka46.com"}


def stable_schedule_url(date: str, meta: str, title: str) -> str:
    key = hashlib.sha1(f"{date}|{meta}|{title}".encode("utf-8")).hexdigest()[:16]
    return f"{normalize_url(SCHEDULE_URL)}#schedule-{key}"


def request_with_retry(session: requests.Session, url: str, timeout: int, retries: int) -> str:
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            response = session.get(url, timeout=timeout)
            if response.status_code in RETRYABLE_STATUS_CODES and attempt < retries:
                wait_seconds = min(30, 2**attempt)
                logging.warning("%s 返回 HTTP %s，%s 秒后重试 (%s/%s)。", url, response.status_code, wait_seconds, attempt, retries)
                time.sleep(wait_seconds)
                continue
            response.raise_for_status()
            response.encoding = response.apparent_encoding or response.encoding
            return response.text
        except requests.RequestException as exc:
            last_error = exc
            if attempt >= retries:
                break
            wait_seconds = min(30, 2**attempt)
            logging.warning("%s 请求失败，%s 秒后重试 (%s/%s)：%s", url, wait_seconds, attempt, retries, exc)
            time.sleep(wait_seconds)
    raise RuntimeError(f"{url} 请求失败：{last_error}")


def soup_from_url(session: requests.Session, url: str, timeout: int, retries: int) -> BeautifulSoup:
    return BeautifulSoup(request_with_retry(session, url, timeout, retries), "lxml")


def link_candidates(soup: BeautifulSoup, path_hint: str) -> list[Tag]:
    anchors = []
    for anchor in soup.find_all("a", href=True):
        href = normalize_url(anchor.get("href", ""))
        if is_official_url(href) and path_hint in urlparse(href).path:
            anchors.append(anchor)
    return anchors


def item_container(anchor: Tag) -> Tag:
    node: Tag = anchor
    for parent in anchor.parents:
        if not isinstance(parent, Tag):
            continue
        if parent.name == "li":
            return parent
        classes = " ".join(parent.get("class", []))
        if any(token in classes.lower() for token in ["item", "article", "post", "news", "blog"]):
            return parent
        node = parent
    return node


def parse_news(soup: BeautifulSoup) -> list[Item]:
    items: dict[str, Item] = {}
    for anchor in link_candidates(soup, "/s/s46/news/detail/"):
        container = item_container(anchor)
        text = clean_text(container.get_text(" ", strip=True))
        match = re.search(r"(.+?)\s+(\d{4}\.\d{2}\.\d{2})\s+(.+)", text)
        if match:
            meta = clean_text(match.group(1))
            date = match.group(2)
            title = clean_text(match.group(3).replace("MORE", ""))
        else:
            date_match = re.search(r"\d{4}\.\d{2}\.\d{2}", text)
            date = date_match.group(0) if date_match else ""
            meta = clean_text(text[: date_match.start()]) if date_match else ""
            title = clean_text(anchor.get_text(" ", strip=True) or text)
        url = normalize_url(anchor["href"])
        if title and url not in items:
            items[url] = Item("NEWS", date, meta, title, url)
    return sort_items(items.values())


def parse_blog(soup: BeautifulSoup) -> list[Item]:
    items: dict[str, Item] = {}
    for anchor in link_candidates(soup, "/s/s46/diary/detail/"):
        container = item_container(anchor)
        text = clean_text(container.get_text(" ", strip=True))
        match = re.search(r"(.+?)\s+(\d{4}/\d{1,2}/\d{1,2})\s+(.+)", text)
        if match:
            meta = clean_text(match.group(1))
            date = normalize_date(match.group(2))
            rest = clean_text(match.group(3).replace("MORE", ""))
            title = infer_blog_title(rest)
        else:
            date_match = re.search(r"\d{4}/\d{1,2}/\d{1,2}", text)
            date = normalize_date(date_match.group(0)) if date_match else ""
            meta = clean_text(text[: date_match.start()]) if date_match else ""
            title = clean_text(anchor.get_text(" ", strip=True) or text)
        url = normalize_url(anchor["href"])
        if title and url not in items:
            items[url] = Item("BLOG", date, meta, title, url)
    return sort_items(items.values())


def infer_blog_title(text: str) -> str:
    text = clean_text(text)
    if not text:
        return "無題"
    # Blog list entries concatenate title and excerpt. The title is usually the short leading phrase.
    sentence_break = re.search(r"[。！？!?]", text)
    if sentence_break and sentence_break.start() <= 40:
        return clean_text(text[: sentence_break.start() + 1])
    return text[:40].rstrip() + ("..." if len(text) > 40 else "")


def normalize_date(date: str) -> str:
    parts = re.split(r"[/.]", date)
    if len(parts) < 3:
        return clean_text(date)
    return f"{int(parts[0]):04d}.{int(parts[1]):02d}.{int(parts[2]):02d}"


def parse_schedule(soup: BeautifulSoup) -> list[Item]:
    text = soup.get_text("\n", strip=True)
    lines = [clean_text(line) for line in text.splitlines() if clean_text(line)]
    items: dict[str, Item] = {}
    date_pattern = re.compile(r"^\d{4}\.\d{2}\.\d{2}(?:\s+\d{1,2}:\d{2}～?)?$")

    for index, line in enumerate(lines):
        if not date_pattern.match(line):
            continue
        if index + 2 >= len(lines):
            continue
        date = line
        meta = lines[index + 1]
        title = lines[index + 2]
        if title in {"メンバー", "MORE"} or meta in {"メンバー", "MORE"}:
            continue
        members = collect_schedule_members(lines[index + 3 : index + 10])
        meta_text = f"{meta} / {members}" if members else meta
        url = stable_schedule_url(date, meta_text, title)
        items[url] = Item("SCHEDULE", date, meta_text, title, url)

    return sort_items(items.values())


def collect_schedule_members(lines: list[str]) -> str:
    if not lines or "メンバー" not in lines:
        return ""
    start = lines.index("メンバー") + 1
    members = []
    for line in lines[start:]:
        if re.match(r"^\d{4}\.\d{2}\.\d{2}", line) or line.startswith("http"):
            break
        if line.endswith("コチラ") or "公式" in line or line in {"メンバー", "MORE"}:
            break
        if len(line) <= 12:
            members.append(line)
    return "、".join(members[:6])


def sort_items(items: Iterable[Item]) -> list[Item]:
    return sorted(items, key=lambda item: (item.date, item.title, item.url), reverse=True)


def filter_grouped_by_date(grouped_items: dict[str, list[Item]], target_date: str) -> dict[str, list[Item]]:
    return {
        section: [item for item in items if item.date.startswith(target_date)]
        for section, items in grouped_items.items()
    }


def init_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pushed_items (
            url TEXT PRIMARY KEY,
            section TEXT NOT NULL,
            title TEXT NOT NULL,
            date TEXT,
            first_seen_at TEXT NOT NULL,
            pushed_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def filter_new_items(conn: sqlite3.Connection, items: list[Item]) -> list[Item]:
    new_items = []
    for item in items:
        exists = conn.execute("SELECT 1 FROM pushed_items WHERE url = ?", (item.url,)).fetchone()
        if not exists:
            new_items.append(item)
    return new_items


def mark_pushed(conn: sqlite3.Connection, items: list[Item]) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    conn.executemany(
        """
        INSERT OR IGNORE INTO pushed_items (url, section, title, date, first_seen_at, pushed_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [(item.url, item.section, item.title, item.date, now, now) for item in items],
    )
    conn.commit()


def item_markdown_line(item: Item) -> str:
    meta = f"｜{item.meta}" if item.meta else ""
    date = item.date or "日期未知"
    return f"- **{date}{meta}** [{item.title}]({item.url})"


def build_markdown(grouped_items: dict[str, list[Item]], empty: bool = False) -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    lines = [f"# 櫻坂46 官方更新 {today}", ""]
    if empty:
        lines.extend(["今日暂无新更新。", ""])
        return "\n".join(lines).strip()

    for section in ["NEWS", "BLOG", "SCHEDULE"]:
        items = grouped_items.get(section, [])
        if not items:
            continue
        lines.extend([f"## {section}", ""])
        for item in items:
            lines.append(item_markdown_line(item))
        lines.append("")
    return "\n".join(lines).strip()


def content_size(content: str) -> int:
    return len(content.encode("utf-8"))


def build_markdown_chunks(grouped_items: dict[str, list[Item]], max_bytes: int) -> list[str]:
    today = datetime.now().strftime("%Y-%m-%d")
    chunks: list[str] = []
    current_lines = [f"# 櫻坂46 官方更新 {today}", ""]
    current_section = ""

    def current_content() -> str:
        return "\n".join(current_lines).strip()

    def flush() -> None:
        content = current_content()
        if content:
            chunks.append(content)

    for section in ["NEWS", "BLOG", "SCHEDULE"]:
        items = grouped_items.get(section, [])
        if not items:
            continue

        section_header = [f"## {section}", ""]
        if current_section != section:
            candidate = [*current_lines, *section_header]
            if current_content() and content_size("\n".join(candidate).strip()) > max_bytes:
                flush()
                current_lines = [f"# 櫻坂46 官方更新 {today}", "", *section_header]
            else:
                current_lines.extend(section_header)
            current_section = section

        for item in items:
            line = item_markdown_line(item)
            candidate = [*current_lines, line]
            if current_content() and content_size("\n".join(candidate).strip()) > max_bytes:
                flush()
                current_lines = [f"# 櫻坂46 官方更新 {today}", "", f"## {section}", "", line]
            else:
                current_lines.append(line)
        current_lines.append("")

    flush()
    return chunks


def push_to_pushplus(title: str, content: str, token: str, timeout: int, part: str = "") -> bool:
    if not token:
        logging.warning("未配置 PUSHPLUS_TOKEN，跳过微信推送。仅在控制台显示内容。")
        print("=" * 60)
        if part:
            print(part)
        print(content)
        print("=" * 60)
        return True

    payload = {
        "token": token,
        "title": f"{title} {part}".strip(),
        "content": content,
        "template": "markdown",
    }
    try:
        response = requests.post(PUSHPLUS_URL, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers={"Content-Type": "application/json"}, timeout=timeout)
        response.raise_for_status()
        result = response.json()
    except Exception as exc:
        logging.error("PushPlus 推送请求失败：%s", exc)
        return False

    if result.get("code") == 200:
        logging.info("PushPlus 推送成功。")
        return True
    logging.error("PushPlus 推送失败：%s", result)
    return False


def push_markdown_chunks(title: str, chunks: list[str], token: str, timeout: int, send_interval: int) -> bool:
    total = len(chunks)
    for index, content in enumerate(chunks, 1):
        part = f"({index}/{total})" if total > 1 else ""
        if total > 1:
            logging.info("准备推送第 %s/%s 条，长度 %s 字符 / %s bytes。", index, total, len(content), content_size(content))
        if not push_to_pushplus(title, content, token, timeout, part):
            return False
        if token and index < total:
            time.sleep(send_interval)
    return True


def fetch_all_items(session: requests.Session, timeout: int, retries: int, enable_schedule: bool) -> dict[str, list[Item]]:
    grouped: dict[str, list[Item]] = {"NEWS": [], "BLOG": [], "SCHEDULE": []}

    try:
        grouped["NEWS"] = parse_news(soup_from_url(session, NEWS_URL, timeout, retries))
        logging.info("NEWS 抓取到 %s 条。", len(grouped["NEWS"]))
    except Exception as exc:
        logging.exception("NEWS 抓取失败：%s", exc)

    try:
        blog_items = parse_blog(soup_from_url(session, BLOG_URL, timeout, retries))
        if not blog_items:
            logging.warning("BLOG 主入口未解析到内容，切换到官方列表兜底。")
            blog_items = parse_blog(soup_from_url(session, BLOG_FALLBACK_URL, timeout, retries))
        grouped["BLOG"] = blog_items
        logging.info("BLOG 抓取到 %s 条。", len(grouped["BLOG"]))
    except Exception as exc:
        logging.exception("BLOG 抓取失败：%s", exc)

    if enable_schedule:
        try:
            grouped["SCHEDULE"] = parse_schedule(soup_from_url(session, SCHEDULE_URL, timeout, retries))
            logging.info("SCHEDULE 抓取到 %s 条。", len(grouped["SCHEDULE"]))
        except Exception as exc:
            logging.exception("SCHEDULE 抓取失败：%s", exc)
    else:
        logging.info("ENABLE_SCHEDULE=false，跳过 SCHEDULE。")

    return grouped


def main() -> int:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    token = os.getenv("PUSHPLUS_TOKEN", "").strip()
    enable_schedule = env_bool("ENABLE_SCHEDULE", True)
    push_only_today = env_bool("PUSH_ONLY_TODAY", True)
    push_empty_message = env_bool("PUSH_EMPTY_MESSAGE", False)
    database_path = os.getenv("DATABASE_PATH", "sakurazaka_push.sqlite3")
    timeout = env_int("REQUEST_TIMEOUT", 20)
    retries = env_int("REQUEST_RETRIES", 3)
    max_content_bytes = env_int("PUSHPLUS_MAX_CONTENT_BYTES", 12000)
    send_interval = env_int("PUSHPLUS_SEND_INTERVAL_SECONDS", 5)
    user_agent = os.getenv("USER_AGENT", "Sakurazaka46DailyPush/1.0 (+https://sakurazaka46.com/)")

    session = requests.Session()
    session.headers.update({"User-Agent": user_agent, "Accept-Language": "ja,en;q=0.8,zh-CN;q=0.6"})

    grouped = fetch_all_items(session, timeout, retries, enable_schedule)
    fetched_items = [item for items in grouped.values() for item in items]
    if not fetched_items:
        logging.error("没有抓取到任何内容，跳过推送和数据库写入。")
        return 1

    if push_only_today:
        target_date = datetime.now().strftime("%Y.%m.%d")
        grouped = filter_grouped_by_date(grouped, target_date)
        logging.info(
            "PUSH_ONLY_TODAY=true，仅处理 %s 的内容：NEWS %s 条，BLOG %s 条，SCHEDULE %s 条。",
            target_date,
            len(grouped.get("NEWS", [])),
            len(grouped.get("BLOG", [])),
            len(grouped.get("SCHEDULE", [])),
        )

    with init_db(database_path) as conn:
        all_items = [item for items in grouped.values() for item in items]
        new_items = filter_new_items(conn, all_items)
        new_grouped = {
            section: [item for item in items if item in new_items]
            for section, items in grouped.items()
        }

        if not new_items:
            logging.info("没有新内容。")
            if not push_empty_message:
                return 0
            content = build_markdown({}, empty=True)
            title = f"櫻坂46 官方更新 {datetime.now().strftime('%Y-%m-%d')}"
            return 0 if push_to_pushplus(title, content, token, timeout) else 1

        chunks = build_markdown_chunks(new_grouped, max_content_bytes)
        title = f"櫻坂46 官方更新 {datetime.now().strftime('%Y-%m-%d')}"
        if not push_markdown_chunks(title, chunks, token, timeout, send_interval):
            return 1
        mark_pushed(conn, new_items)
        logging.info("已记录 %s 条新内容。", len(new_items))
    return 0


if __name__ == "__main__":
    sys.exit(main())
