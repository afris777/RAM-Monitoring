"""
report.py — build the price-history report.

Can be run standalone:
    python report.py

Prints a table:
    Model | Current Price | Shop | Price 7 days ago | Change (%)
"""

from database import get_latest_prices, get_price_7_days_ago


def get_report_data() -> list[dict]:
    """
    Return a list of dicts, one per module, with keys:
        model_name, url, price, shop, timestamp,
        price_7d, shop_7d, timestamp_7d, change_pct
    """
    rows = get_latest_prices()
    report = []
    for row in rows:
        entry = dict(row)
        old = get_price_7_days_ago(row["model_name"])
        if old and old["price"] and entry["price"]:
            change_pct = (entry["price"] - old["price"]) / old["price"] * 100
            entry["price_7d"] = old["price"]
            entry["shop_7d"] = old["shop"]
            entry["timestamp_7d"] = old["timestamp"]
            entry["change_pct"] = change_pct
        else:
            entry["price_7d"] = None
            entry["shop_7d"] = None
            entry["timestamp_7d"] = None
            entry["change_pct"] = None
        report.append(entry)
    return report


def print_report() -> None:
    data = get_report_data()
    if not data:
        print("No data in database yet.")
        return

    col_widths = {
        "model":   44,
        "price":   13,
        "shop":    28,
        "price7d": 13,
        "change":  10,
    }

    header = (
        f"{'Model':<{col_widths['model']}} "
        f"{'Current €':>{col_widths['price']}} "
        f"{'Shop':<{col_widths['shop']}} "
        f"{'7d ago €':>{col_widths['price7d']}} "
        f"{'Change':>{col_widths['change']}}"
    )
    sep = "-" * len(header)
    print(sep)
    print(header)
    print(sep)

    for row in data:
        price_str = f"{row['price']:.2f}" if row["price"] is not None else "N/A"
        shop_str = (row["shop"] or "")[:col_widths["shop"]]
        price7d_str = f"{row['price_7d']:.2f}" if row["price_7d"] is not None else "—"
        change_str = (
            f"{row['change_pct']:+.1f}%"
            if row["change_pct"] is not None
            else "—"
        )
        print(
            f"{row['model_name']:<{col_widths['model']}} "
            f"{price_str:>{col_widths['price']}} "
            f"{shop_str:<{col_widths['shop']}} "
            f"{price7d_str:>{col_widths['price7d']}} "
            f"{change_str:>{col_widths['change']}}"
        )

    print(sep)


if __name__ == "__main__":
    print_report()
