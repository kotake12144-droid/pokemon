"""
eBay Sold Listings Scraper
ログイン不要でSold Itemsページから直近N日のデータを取得する
"""

import asyncio
import csv
import re
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple, List
from playwright.async_api import async_playwright

DAYS = 7
OUTPUT_FILE = f"ebay_sold_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"

# ユーザー指定のURLベース
BASE_URL = (
    "https://www.ebay.com/sch/i.html"
    "?_nkw=pokemon%20psa%E3%80%8010%20japanese"  # psa　10（全角スペース）
    "&_sacat=0"
    "&_from=R40"
    "&LH_Complete=1"
    "&LH_Sold=1"       # 売れた商品のみ
    "&_udlo=1,00,000"  # 最低価格フィルター
    "&rt=nc"
    "&_sop=13"         # 最近終了した順
    "&_ipg=240"        # 1ページ最大240件
    "&_pgn={page}"
)

CUTOFF = datetime.now(timezone.utc) - timedelta(days=DAYS)

# 月名マッピング（日本語 / 英語）
JP_MONTHS = {
    "1月": 1, "2月": 2, "3月": 3, "4月": 4,
    "5月": 5, "6月": 6, "7月": 7, "8月": 8,
    "9月": 9, "10月": 10, "11月": 11, "12月": 12,
}


def parse_sold_date(text: str) -> Optional[datetime]:
    """
    eBayの日付テキストをdatetimeに変換
    日本語: "販売済み  2026年4月14日"
    英語:   "Sold  Apr 14, 2026"
    """
    text = re.sub(r"(販売済み|Sold)\s*", "", text).strip()

    # 日本語形式: 2026年4月14日
    jp_match = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", text)
    if jp_match:
        y, m, d = int(jp_match.group(1)), int(jp_match.group(2)), int(jp_match.group(3))
        return datetime(y, m, d, tzinfo=timezone.utc)

    # 英語形式: Apr 14, 2026
    for fmt in ("%b %d, %Y", "%B %d, %Y"):
        try:
            dt = datetime.strptime(text, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue

    return None


def parse_price(text: str) -> Tuple[Optional[float], str]:
    """
    価格テキストをfloatとcurrencyに変換
    "$1,234.56" → (1234.56, "USD")
    "15,786 円" → (15786.0, "JPY")
    """
    text = text.strip()
    if "円" in text:
        nums = re.sub(r"[^\d]", "", text)
        return (float(nums) if nums else None, "JPY")
    else:
        match = re.search(r"[\d,]+\.?\d*", text.replace(",", ""))
        return (float(match.group()) if match else None, "USD")


async def scrape_page(page, url: str) -> Tuple[List[dict], bool]:
    """
    1ページ分のデータを取得する
    Returns: (items, stop_flag)
    stop_flag=True → 3日より古いアイテムが出たので終了
    """
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    await asyncio.sleep(3)

    # ul.srp-results の直接の子li要素を取得
    ul = await page.query_selector("ul.srp-results")
    if not ul:
        return [], False
    listings = await ul.query_selector_all(":scope > li")

    items_data = []
    stop = False

    for listing in listings:
        # s-card クラスがないものはスキップ（プレースホルダー等）
        li_class = await listing.get_attribute("class") or ""
        if "s-card" not in li_class:
            continue

        # タイトル
        title_el = await listing.query_selector(".s-card__title .su-styled-text")
        title = (await title_el.inner_text()).strip() if title_el else ""
        if not title:
            continue

        # URL（クエリパラメータ除去）
        link_el = await listing.query_selector("a.s-card__link")
        href = await link_el.get_attribute("href") if link_el else ""
        item_url = href.split("?")[0] if href else ""
        item_id = re.search(r"/itm/(\d+)", item_url)
        item_id = item_id.group(1) if item_id else ""

        # 価格
        price_el = await listing.query_selector(".s-card__price")
        price_text = (await price_el.inner_text()).strip() if price_el else ""
        price, currency = parse_price(price_text)

        # 販売日
        date_el = await listing.query_selector(".s-card__caption .su-styled-text")
        date_text = (await date_el.inner_text()).strip() if date_el else ""
        sold_date = parse_sold_date(date_text)

        # 3日より古ければ終了フラグ
        if sold_date and sold_date < CUTOFF:
            stop = True
            break

        # コンディション
        cond_el = await listing.query_selector(".s-card__subtitle .su-styled-text")
        condition = (await cond_el.inner_text()).strip() if cond_el else ""

        items_data.append({
            "item_id": item_id,
            "title": title,
            "price": price,
            "currency": currency,
            "sold_date": sold_date.strftime("%Y-%m-%d") if sold_date else "",
            "condition": condition,
            "url": item_url,
        })

    return items_data, stop


async def main():

    all_items = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
            locale="en-US",
        )
        page = await context.new_page()

        print(f"検索中: 'pokemon psa　10 japanese'（直近{DAYS}日 / 最低価格フィルターあり）")

        for page_num in range(1, 6):  # 最大5ページ
            url = BASE_URL.format(page=page_num)
            print(f"  ページ {page_num} 取得中...", end=" ", flush=True)

            try:
                items, stop = await scrape_page(page, url)
            except Exception as e:
                print(f"エラー: {e}")
                break

            all_items.extend(items)
            print(f"{len(items)}件")

            if stop or len(items) == 0:
                break

        await browser.close()

    if not all_items:
        print("データが見つかりませんでした")
        return

    # 重複除去（item_idで）
    seen = set()
    unique_items = []
    for item in all_items:
        if item["item_id"] not in seen:
            seen.add(item["item_id"])
            unique_items.append(item)
    all_items = unique_items

    # CSVに保存
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=all_items[0].keys())
        writer.writeheader()
        writer.writerows(all_items)

    # サマリー表示
    prices = [item["price"] for item in all_items if item["price"]]
    currency = all_items[0]["currency"] if all_items else "USD"
    symbol = "¥" if currency == "JPY" else "$"

    print(f"\n{'='*45}")
    print(f"取得件数 : {len(all_items)}件")
    if prices:
        avg = sum(prices) / len(prices)
        print(f"平均価格 : {symbol}{avg:,.0f}")
        print(f"最高価格 : {symbol}{max(prices):,.0f}")
        print(f"最低価格 : {symbol}{min(prices):,.0f}")
    print(f"保存先   : {OUTPUT_FILE}")
    print(f"{'='*45}\n")

    print("【価格上位10件】")
    sorted_items = sorted(all_items, key=lambda x: x["price"] or 0, reverse=True)
    for item in sorted_items[:10]:
        print(f"  {symbol}{item['price']:>10,.0f} | {item['title'][:50]}")


if __name__ == "__main__":
    asyncio.run(main())
