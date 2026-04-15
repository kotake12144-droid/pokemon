"""
pokeca-chart.com スキャナー
mode=5 (価格高騰順) の上位100件について
美品価格 vs PSA10直近価格の差を比較する
"""

import asyncio
import csv
import re
import os
from datetime import datetime
from playwright.async_api import async_playwright
from dotenv import load_dotenv

load_dotenv()

MODE = 5
TOP_N = 100
OUTPUT_FILE = f"pokeca_scan_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"


def parse_price(text: str):
    """'16,000円' → 16000"""
    nums = re.sub(r"[^\d]", "", text)
    return int(nums) if nums else None


async def get_card_links(page, top_n: int) -> list[dict]:
    """
    一覧ページをスクロールして上位N件のリンクを取得
    """
    await page.goto(f"https://pokeca-chart.com/all-card?mode={MODE}", wait_until="domcontentloaded", timeout=30000)
    await asyncio.sleep(3)

    # スクロールして必要件数を読み込む
    while True:
        cards = await page.query_selector_all(".cp_card")
        if len(cards) >= top_n:
            break
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(2)

    cards = await page.query_selector_all(".cp_card")
    print(f"  一覧読み込み完了: {len(cards)}件中 上位{top_n}件を処理")

    results = []
    for card in cards[:top_n]:
        rank_el = await card.query_selector(".category p")
        rank_text = (await rank_el.inner_text()).strip() if rank_el else ""
        rank = int(re.sub(r"[^\d]", "", rank_text)) if rank_text else 0

        link_el = await card.query_selector("a")
        href = await link_el.get_attribute("href") if link_el else ""

        results.append({"rank": rank, "url": href})

    return results


async def get_card_prices(page, url: str) -> dict:
    """
    詳細ページから カード名・美品価格・PSA10価格 を取得
    """
    await page.goto(url, wait_until="domcontentloaded", timeout=20000)
    await asyncio.sleep(1)

    # カード名
    h1 = await page.query_selector("h1.entry-title")
    name = (await h1.inner_text()).strip() if h1 else url.split("/")[-1]

    # 価格テーブル（1つ目）
    # 構造: 行0=ヘッダー(美品/キズあり/PSA10), 行1=データ数, 行2=直近価格, 行3=最高価格
    mint_price = None
    psa10_price = None

    tables = await page.query_selector_all("table")
    if tables:
        rows = await tables[0].query_selector_all("tr")
        if len(rows) >= 3:
            # 行0: ヘッダー確認
            header_cells = await rows[0].query_selector_all("th, td")
            headers = [await c.inner_text() for c in header_cells]

            # 行2: 直近価格
            price_cells = await rows[2].query_selector_all("th, td")
            price_texts = [await c.inner_text() for c in price_cells]

            # ヘッダーと対応させて取得
            for i, h in enumerate(headers):
                if i < len(price_texts):
                    val = parse_price(price_texts[i])
                    if "美品" in h:
                        mint_price = val
                    elif "PSA10" in h or "PSA" in h:
                        psa10_price = val

    return {
        "name": name,
        "mint_price": mint_price,
        "psa10_price": psa10_price,
        "url": url,
    }


async def main():
    results = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        # Step1: 上位100件のリンクを取得
        print(f"[1/2] 一覧ページ読み込み中 (mode={MODE})...")
        card_links = await get_card_links(page, TOP_N)

        # Step2: 各詳細ページから価格を取得
        print(f"[2/2] 詳細ページ巡回中 ({len(card_links)}件)...")
        for i, card in enumerate(card_links, 1):
            try:
                data = await get_card_prices(page, card["url"])
                data["rank"] = card["rank"]

                mint = data["mint_price"]
                psa10 = data["psa10_price"]
                data["diff"] = (psa10 - mint) if (mint and psa10) else None

                results.append(data)
                status = f"¥{mint:,} → ¥{psa10:,} (差: ¥{data['diff']:,})" if data["diff"] else "価格なし"
                print(f"  {i:>3}位 {data['name'][:30]:<30} {status}")
            except Exception as e:
                print(f"  {i:>3}位 エラー: {card['url']} ({e})")

        await browser.close()

    # 差額でソート（大きい順）
    sortable = [r for r in results if r["diff"] is not None]
    sortable.sort(key=lambda x: x["diff"], reverse=True)

    # CSV保存
    fields = ["rank", "name", "mint_price", "psa10_price", "diff", "url"]
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for r in sortable:
            writer.writerow({k: r.get(k) for k in fields})

    # 結果表示
    print(f"\n{'='*60}")
    print(f"美品 vs PSA10 差額ランキング（上位20件）")
    print(f"{'='*60}")
    print(f"{'順位':<5} {'差額':>10} {'美品':>10} {'PSA10':>10}  カード名")
    print(f"{'-'*60}")
    for i, r in enumerate(sortable[:20], 1):
        print(
            f"{i:<5} "
            f"¥{r['diff']:>9,} "
            f"¥{r['mint_price']:>9,} "
            f"¥{r['psa10_price']:>9,}  "
            f"{r['name'][:35]}"
        )
    print(f"\n保存先: {OUTPUT_FILE}")
    notify_discord(sortable, len(results))


DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK", "")


def notify_discord(sortable: list[dict], total: int):
    """Discord Webhookに結果を送る"""
    import requests

    if not sortable:
        return

    today = datetime.now().strftime("%Y/%m/%d %H:%M")

    lines = []
    for i, r in enumerate(sortable[:20], 1):
        lines.append(
            f"`{i:>2}.` **{r['name'][:28]}**\n"
            f"　　美品 ¥{r['mint_price']:,} → PSA10 ¥{r['psa10_price']:,}　差額 **¥{r['diff']:,}**"
        )

    payload = {
        "username": "ポケカ相場Bot",
        "embeds": [
            {
                "title": "📊 美品 vs PSA10 差額ランキング TOP20",
                "description": "\n".join(lines),
                "color": 0xFFCC00,
                "footer": {"text": f"スキャン日時: {today}　対象: mode=5 上位{total}件"},
            }
        ],
    }

    resp = requests.post(DISCORD_WEBHOOK, json=payload)
    if resp.status_code == 204:
        print("Discord通知: 送信完了")
    else:
        print(f"Discord通知: エラー {resp.status_code} {resp.text}")


if __name__ == "__main__":
    asyncio.run(main())
