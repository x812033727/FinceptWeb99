"""Unit tests for the generic RSS/Atom parser (`data.rss_connector`).

Pins: RSS 2.0 + Atom item extraction, RFC822 + ISO date normalisation,
limit, malformed-item skipping, and the defusedxml security guard
(entity-expansion / DTD payloads must not blow up or parse).
"""
from __future__ import annotations

from data.rss_connector import _parse_date, _parse_items

RSS_2 = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <title>Feed</title>
  <item>
    <title>台積電法說會優於預期</title>
    <link>https://example.com/a?utm_source=x</link>
    <description>內文摘要</description>
    <pubDate>Sat, 12 Jul 2026 09:10:02 +0800</pubDate>
  </item>
  <item>
    <title>沒有連結的新聞</title>
    <description>should be skipped — no link</description>
    <pubDate>Sat, 12 Jul 2026 08:00:00 +0000</pubDate>
  </item>
  <item>
    <title>壞日期</title>
    <link>https://example.com/b</link>
    <pubDate>not-a-date</pubDate>
  </item>
</channel></rss>"""

ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>Atom headline</title>
    <link href="https://example.com/atom1"/>
    <summary>atom summary</summary>
    <published>2026-07-12T01:02:03Z</published>
  </entry>
</feed>"""

# Classic billion-laughs entity-expansion bomb — defusedxml must refuse.
BILLION_LAUGHS = """<?xml version="1.0"?>
<!DOCTYPE lolz [
  <!ENTITY lol "lol">
  <!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
  <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">
]>
<rss version="2.0"><channel><item><title>&lol3;</title>
<link>https://x.com/y</link><pubDate>Sat, 12 Jul 2026 09:10:02 +0800</pubDate>
</item></channel></rss>"""


def test_rss2_parses_valid_items_and_skips_bad():
    items = _parse_items(RSS_2, limit=10)
    # Only the first item is valid: item 2 has no link, item 3 has an
    # unparseable date → both dropped.
    assert len(items) == 1
    it = items[0]
    assert it["title"] == "台積電法說會優於預期"
    assert it["link"] == "https://example.com/a?utm_source=x"
    assert it["description"] == "內文摘要"
    assert it["published_at"].startswith("2026-07-12T01:10:02")  # +0800 → UTC


def test_atom_entry_with_href_link():
    items = _parse_items(ATOM, limit=10)
    assert len(items) == 1
    assert items[0]["link"] == "https://example.com/atom1"
    assert items[0]["description"] == "atom summary"
    assert items[0]["published_at"] == "2026-07-12T01:02:03+00:00"


def test_limit_caps_items():
    many = "".join(
        f"<item><title>t{i}</title><link>https://x/{i}</link>"
        f"<pubDate>Sat, 12 Jul 2026 09:10:0{i} +0000</pubDate></item>"
        for i in range(5)
    )
    xml = f'<rss version="2.0"><channel>{many}</channel></rss>'
    assert len(_parse_items(xml, limit=2)) == 2


def test_malformed_xml_returns_empty():
    assert _parse_items("<rss><broken", limit=5) == []


def test_defusedxml_blocks_entity_expansion():
    # defusedxml raises EntitiesForbidden; _parse_items catches it and
    # returns [] rather than expanding the bomb or crashing.
    assert _parse_items(BILLION_LAUGHS, limit=5) == []


def test_parse_date_rfc822_and_iso():
    assert _parse_date("Sat, 12 Jul 2026 09:10:02 +0800").startswith(
        "2026-07-12T01:10:02"
    )
    assert _parse_date("2026-07-12T01:02:03Z") == "2026-07-12T01:02:03+00:00"
    assert _parse_date("garbage") is None
    assert _parse_date("") is None
