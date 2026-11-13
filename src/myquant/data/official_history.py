from __future__ import annotations

import csv
import hashlib
import html
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import unquote, urljoin, urlparse

import requests


CSINDEX_HOME = "https://www.csindex.com.cn"
CSINDEX_API = f"{CSINDEX_HOME}/csindex-home"
OFFICIAL_HOSTS = {"www.csindex.com.cn", "csindex.com.cn", "oss-ch.csindex.com.cn"}
ATTACHMENT_SUFFIXES = {".csv", ".doc", ".docx", ".pdf", ".xls", ".xlsx", ".zip"}

# These are products whose names contain "沪深300" but whose membership is not the
# membership of the parent 000300 index.
DERIVATIVE_TITLE_TERMS = (
    "精明",
    "行业指数",
    "风格指数",
    "主题指数",
    "价值指数",
    "相对风格",
    "下行风险",
    "备选名单",
)

# Hard repository-level safety gate. CSIndex began returning access restrictions
# during historical-detail collection. Re-enable only through a reviewed source
# change after the restriction has cleared and the applicable terms have been
# checked; there is deliberately no CLI flag or environment-variable bypass.
AUTOMATED_CSINDEX_COLLECTION_PAUSED = True
AUTOMATED_CSINDEX_COLLECTION_PAUSE_REASON = (
    "中证公告详情与附件的自动采集已暂停：远端接口正在限制访问。"
    "请勿通过代理轮换、验证码规避或其他绕过方式继续请求；"
    "待限制解除并重新确认使用条件后，再通过代码评审解除暂停。"
)


class AutomatedCollectionPausedError(RuntimeError):
    """Raised before any automated CSIndex network request while collection is paused."""


def _ensure_automated_collection_allowed() -> None:
    if AUTOMATED_CSINDEX_COLLECTION_PAUSED:
        raise AutomatedCollectionPausedError(AUTOMATED_CSINDEX_COLLECTION_PAUSE_REASON)


@dataclass(frozen=True)
class OfficialAnnouncement:
    announcement_id: int
    publish_date: str
    title: str
    notice_type: str
    detail_url: str


def repair_utf8_mojibake(value: Any) -> Any:
    """Repair the legacy API's occasional UTF-8-as-Latin-1 response strings."""
    if not isinstance(value, str) or not value:
        return value
    try:
        repaired = value.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return value
    original_han = len(re.findall(r"[\u4e00-\u9fff]", value))
    repaired_han = len(re.findall(r"[\u4e00-\u9fff]", repaired))
    return repaired if repaired_han > original_han else value


def is_base_csi300_announcement(title: str) -> bool:
    """Return True only for notices that change or enumerate the parent CSI 300."""
    title = repair_utf8_mojibake(title).strip()
    if "沪深300" not in title:
        return False
    if any(term in title for term in DERIVATIVE_TITLE_TERMS):
        return False
    return (
        "沪深300指数样本股名单" in title
        or title.startswith("关于调整沪深300")
        or title.startswith("关于对沪深300指数样本股")
        or title.startswith("关于沪深300")
    )


def extract_official_attachment_urls(detail: dict[str, Any]) -> list[str]:
    """Extract downloadable files while rejecting non-official and navigation links."""
    candidates: list[str] = []
    content = html.unescape(str(detail.get("content") or ""))
    candidates.extend(re.findall(r"href\s*=\s*[\"']([^\"']+)[\"']", content, flags=re.I))

    for enclosure in detail.get("enclosureList") or []:
        if isinstance(enclosure, dict):
            candidates.extend(
                str(value) for value in enclosure.values() if isinstance(value, str)
            )

    result: list[str] = []
    for candidate in candidates:
        url = urljoin(CSINDEX_HOME, repair_utf8_mojibake(candidate).strip())
        parsed = urlparse(url)
        suffix = Path(unquote(parsed.path)).suffix.lower()
        if parsed.hostname not in OFFICIAL_HOSTS or suffix not in ATTACHMENT_SUFFIXES:
            continue
        if url not in result:
            result.append(url)
    return result


def embedded_security_codes(content: str) -> list[str]:
    """Return ordered, unique six-digit A-share codes embedded in an announcement."""
    seen: set[str] = set()
    result: list[str] = []
    for code in re.findall(r"(?<!\d)(?:000|001|002|003|300|301|600|601|603|605|688|689)\d{3}(?!\d)", content):
        if code not in seen:
            seen.add(code)
            result.append(code)
    return result


class CSI300OfficialHistoryClient:
    """Discover and archive public CSI 300 membership notices from CSIndex."""

    def __init__(
        self,
        session: requests.Session | None = None,
        *,
        request_spacing_seconds: float = 1.0,
        max_attempts: int = 4,
    ) -> None:
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": "MyQuantCSI300/0.1 research"})
        self.request_spacing_seconds = request_spacing_seconds
        self.max_attempts = max_attempts
        self._last_request_at = 0.0

    def query_year(self, year: int) -> list[OfficialAnnouncement]:
        payload = {
            "lang": "cn",
            "searchInput": "沪深300",
            "startDate": f"{year}-01-01",
            "endDate": f"{year}-12-31",
            "page": {"desc": "", "key": "", "page": 1, "rows": 200, "sortBy": ""},
        }
        payload_json = self._request_json(
            "POST",
            f"{CSINDEX_API}/announcement/queryAnnouncementByVo",
            json=payload,
        )
        if str(payload_json.get("code")) != "200":
            raise RuntimeError(f"CSIndex announcement query failed: {payload_json.get('msg')}")

        announcements: list[OfficialAnnouncement] = []
        for item in payload_json.get("data") or []:
            title = repair_utf8_mojibake(item.get("title") or "")
            notice_type = item.get("noticeType") or ""
            if notice_type != "announcement" or not is_base_csi300_announcement(title):
                continue
            announcement_id = int(item["id"])
            announcements.append(
                OfficialAnnouncement(
                    announcement_id=announcement_id,
                    publish_date=item.get("publishDate") or "",
                    title=title,
                    notice_type=notice_type,
                    detail_url=f"{CSINDEX_HOME}/#/about/newsDetail?id={announcement_id}",
                )
            )
        return announcements

    def announcement_detail(self, announcement_id: int) -> dict[str, Any]:
        payload = self._request_json(
            "GET",
            f"{CSINDEX_API}/announcement/queryAnnouncementById",
            params={"id": announcement_id},
        )
        if str(payload.get("code")) != "200" or not payload.get("data"):
            raise RuntimeError(
                f"CSIndex announcement detail {announcement_id} failed: {payload.get('msg')}"
            )
        return _repair_tree(payload["data"])

    def download(self, url: str) -> bytes:
        return self._request("GET", url, timeout=60).content

    def _request_json(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        return self._request(method, url, **kwargs).json()

    def _request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        _ensure_automated_collection_allowed()
        kwargs.setdefault("timeout", 30)
        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            wait_for_spacing = self.request_spacing_seconds - (
                time.monotonic() - self._last_request_at
            )
            if wait_for_spacing > 0:
                time.sleep(wait_for_spacing)
            response = self.session.request(method, url, **kwargs)
            self._last_request_at = time.monotonic()
            if response.status_code not in {429, 500, 502, 503, 504}:
                response.raise_for_status()
                return response
            last_error = requests.HTTPError(
                f"HTTP {response.status_code} from {url}", response=response
            )
            if attempt < self.max_attempts:
                retry_after = response.headers.get("Retry-After")
                delay = float(retry_after) if retry_after and retry_after.isdigit() else 5.0 * attempt
                time.sleep(min(delay, 30.0))
        assert last_error is not None
        raise last_error


def archive_official_history(
    output_dir: Path,
    catalog_path: Path,
    start_year: int = 2005,
    end_year: int = 2026,
    *,
    client: CSI300OfficialHistoryClient | None = None,
    download_attachments: bool = True,
    log: Callable[[str], None] = print,
) -> list[dict[str, Any]]:
    """Discover official notices, archive raw details/files, and write an audit catalog."""
    _ensure_automated_collection_allowed()
    if end_year < start_year:
        raise ValueError("end_year must be greater than or equal to start_year")

    client = client or CSI300OfficialHistoryClient()
    detail_dir = output_dir / "details"
    attachment_dir = output_dir / "attachments"
    detail_dir.mkdir(parents=True, exist_ok=True)
    attachment_dir.mkdir(parents=True, exist_ok=True)

    announcements: list[OfficialAnnouncement] = []
    years = list(range(start_year, end_year + 1))
    for position, year in enumerate(years, start=1):
        found = client.query_year(year)
        announcements.extend(found)
        log(f"[{position}/{len(years)}] official catalog {year}: base CSI300 notices={len(found)}")

    # The old site contains duplicate republications. Keep every official record in the
    # archive; deduplication of effective events happens only after content review.
    announcements = sorted(
        {item.announcement_id: item for item in announcements}.values(),
        key=lambda item: (item.publish_date, item.announcement_id),
    )
    rows: list[dict[str, Any]] = []
    total = len(announcements)
    for position, announcement in enumerate(announcements, start=1):
        detail_name = f"{announcement.publish_date}_{announcement.announcement_id}.json"
        existing_detail_path = detail_dir / detail_name
        try:
            if existing_detail_path.exists():
                raw = existing_detail_path.read_bytes()
                detail = json.loads(raw.decode("utf-8-sig"))
                detail_path = existing_detail_path
                detail_origin = "cache"
            else:
                detail = client.announcement_detail(announcement.announcement_id)
                raw = json.dumps(detail, ensure_ascii=False, indent=2).encode("utf-8")
                detail_path = _write_versioned(existing_detail_path, raw)
                detail_origin = "download"
        except (requests.RequestException, RuntimeError, ValueError) as exc:
            rows.append(
                {
                    "announcement_id": announcement.announcement_id,
                    "publish_date": announcement.publish_date,
                    "title": announcement.title,
                    "detail_url": announcement.detail_url,
                    "detail_archive_path": "",
                    "detail_sha256": "",
                    "embedded_code_count": 0,
                    "attachment_count": 0,
                    "attachment_urls": "",
                    "attachment_archive_paths": "",
                    "attachment_sha256": "",
                    "review_status": "download_error",
                    "error": str(exc),
                }
            )
            log(
                f"[{position}/{total}] ERROR detail {announcement.publish_date} "
                f"id={announcement.announcement_id}: {exc}"
            )
            _write_catalog(catalog_path, rows)
            continue
        detail_hash = hashlib.sha256(raw).hexdigest()
        urls = extract_official_attachment_urls(detail)
        saved_files: list[str] = []
        attachment_hashes: list[str] = []
        attachment_errors: list[str] = []

        if download_attachments:
            for ordinal, url in enumerate(urls, start=1):
                suffix = Path(unquote(urlparse(url).path)).suffix.lower()
                try:
                    payload = client.download(url)
                    name = (
                        f"{announcement.publish_date}_{announcement.announcement_id}_"
                        f"{ordinal:02d}{suffix}"
                    )
                    path = _write_versioned(attachment_dir / name, payload)
                    saved_files.append(str(path))
                    attachment_hashes.append(hashlib.sha256(payload).hexdigest())
                except requests.RequestException as exc:
                    attachment_errors.append(f"{url}: {exc}")

        content = str(detail.get("content") or "")
        rows.append(
            {
                "announcement_id": announcement.announcement_id,
                "publish_date": announcement.publish_date,
                "title": announcement.title,
                "detail_url": announcement.detail_url,
                "detail_archive_path": str(detail_path),
                "detail_sha256": detail_hash,
                "embedded_code_count": len(embedded_security_codes(content)),
                "attachment_count": len(urls),
                "attachment_urls": "|".join(urls),
                "attachment_archive_paths": "|".join(saved_files),
                "attachment_sha256": "|".join(attachment_hashes),
                "review_status": "download_error" if attachment_errors else "pending",
                "error": " | ".join(attachment_errors),
            }
        )
        log(
            f"[{position}/{total}] detail {announcement.publish_date} id={announcement.announcement_id}: "
            f"source={detail_origin}, embedded_codes={rows[-1]['embedded_code_count']}, "
            f"attachments={len(urls)}"
        )
        _write_catalog(catalog_path, rows)

    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    _write_catalog(catalog_path, rows)
    log(
        f"[complete] official notices={len(rows)}, attachments="
        f"{sum(row['attachment_count'] for row in rows)}, catalog={catalog_path}"
    )
    return rows


def _repair_tree(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _repair_tree(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_repair_tree(item) for item in value]
    return repair_utf8_mojibake(value)


def _write_versioned(path: Path, payload: bytes) -> Path:
    if not path.exists():
        path.write_bytes(payload)
        return path
    if path.read_bytes() == payload:
        return path
    digest = hashlib.sha256(payload).hexdigest()[:12]
    revision = path.with_name(f"{path.stem}.revision-{digest}{path.suffix}")
    if not revision.exists():
        revision.write_bytes(payload)
    return revision


def _write_catalog(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    rows = list(rows)
    fieldnames = [
        "announcement_id",
        "publish_date",
        "title",
        "detail_url",
        "detail_archive_path",
        "detail_sha256",
        "embedded_code_count",
        "attachment_count",
        "attachment_urls",
        "attachment_archive_paths",
        "attachment_sha256",
        "review_status",
        "error",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
