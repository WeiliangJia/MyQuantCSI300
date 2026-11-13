from pathlib import Path

import pytest

from myquant.data.official_history import (
    AutomatedCollectionPausedError,
    CSI300OfficialHistoryClient,
    archive_official_history,
    embedded_security_codes,
    extract_official_attachment_urls,
    is_base_csi300_announcement,
    repair_utf8_mojibake,
)


def test_base_csi300_title_filter_excludes_derivatives() -> None:
    assert is_base_csi300_announcement("关于调整沪深300和中证香港100等指数样本股的公告")
    assert is_base_csi300_announcement("沪深300指数样本股名单（2005年7月1日起生效）")
    assert not is_base_csi300_announcement("关于调整沪深300精明等指数样本股的公告")
    assert not is_base_csi300_announcement("沪深300行业指数样本股调整名单")


def test_extracts_only_official_downloadable_attachments() -> None:
    detail = {
        "content": (
            '<a href="https://oss-ch.csindex.com.cn/notice/list.xlsx">名单</a>'
            '<a href="https://example.com/list.xlsx">镜像</a>'
            '<a href="http://www.csindex.com.cn">首页</a>'
        ),
        "enclosureList": [],
    }
    assert extract_official_attachment_urls(detail) == [
        "https://oss-ch.csindex.com.cn/notice/list.xlsx"
    ]


def test_embedded_security_codes_are_ordered_and_unique() -> None:
    content = "000001 深发展 600000 浦发银行 000001 重复 688001 华兴源创"
    assert embedded_security_codes(content) == ["000001", "600000", "688001"]


def test_repairs_legacy_utf8_mojibake() -> None:
    mojibake = "沪深300".encode("utf-8").decode("latin-1")
    assert repair_utf8_mojibake(mojibake) == "沪深300"


def test_automatic_history_collection_is_paused_before_local_or_network_work(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "raw"
    catalog_path = tmp_path / "catalog.csv"

    with pytest.raises(AutomatedCollectionPausedError, match="自动采集已暂停"):
        archive_official_history(output_dir, catalog_path)

    assert not output_dir.exists()
    assert not catalog_path.exists()


def test_client_network_request_is_blocked_while_collection_is_paused() -> None:
    client = CSI300OfficialHistoryClient(request_spacing_seconds=0)

    with pytest.raises(AutomatedCollectionPausedError, match="自动采集已暂停"):
        client.announcement_detail(2714)
