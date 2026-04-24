"""
公開資訊觀測站 (MOPS) scraper — monthly revenue fallback.
Parses HTML table from the MOPS AJAX endpoint.
Results are in thousands of NTD.
"""
import httpx
from bs4 import BeautifulSoup
from typing import Any


async def get_monthly_revenue(symbol: str, year: int, month: int) -> list[dict[str, Any]]:
    """
    Fetch monthly revenue for a single stock from MOPS.
    year/month are CE (e.g., 2026, 4).
    """
    roc_year = year - 1911
    url = "https://mops.twse.com.tw/mops/web/ajax_t05st10_ifrs"
    payload = {
        "encodeURIComponent": "1",
        "step": "1",
        "firstin": "1",
        "off": "1",
        "queryName": "co_id",
        "inpuType": "co_id",
        "TYPEK": "all",
        "isnew": "false",
        "co_id": symbol,
        "year": str(roc_year),
        "month": str(month).zfill(2),
    }

    async with httpx.AsyncClient(timeout=20.0) as c:
        r = await c.post(url, data=payload, headers={"Content-Type": "application/x-www-form-urlencoded"})
        r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")
    rows = []

    table = soup.find("table", class_="hasBorder")
    if not table:
        return rows

    _headers = [th.get_text(strip=True) for th in table.find_all("th")]
    for tr in table.find_all("tr")[1:]:
        cells = [td.get_text(strip=True) for td in tr.find_all("td")]
        if len(cells) < 3:
            continue
        try:
            revenue_str = cells[2].replace(",", "") if len(cells) > 2 else "0"
            revenue = int(revenue_str) if revenue_str.isdigit() else 0
            rows.append({
                "symbol":  symbol,
                "date":    f"{year}-{month:02d}-01",
                "revenue": revenue,  # thousands NTD
                "source":  "mops",
            })
        except Exception:
            continue

    return rows
