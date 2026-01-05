import csv
import json
from pathlib import Path
from datetime import datetime

# Rocket Pool L2 pools owed alliance fees from 2025-06-04
ROCKET_POOL_L2_POOLS = {
    "0x5418a64e0cdb20548acb394f5d00a089baf02161": "Arbitrum",
    "0xb7b8b3afc010169779c5c2385ec0eb0477fe3347": "Base",
    "0x870c0af8a1af0b58b4b0bd31ce4fe72864ae45be": "Optimism",
}

PARTNER_SHARE_PCT = 0.175  # 17.5%
START_DATE = datetime(2025, 6, 4)

def parse_date_from_filename(filename: str) -> tuple[datetime, datetime]:
    parts = filename.replace(".csv", "").split("_")
    dates = [p for p in parts if len(p) == 10 and p[4] == "-" and p[7] == "-"]
    if len(dates) >= 2:
        start = datetime.strptime(dates[0], "%Y-%m-%d")
        end = datetime.strptime(dates[1], "%Y-%m-%d")
        return start, end
    return None, None

def main():
    incentives_dir = Path("fee_allocator/allocations/incentives")
    v3_files = sorted([f for f in incentives_dir.glob("v3_incentives_*.csv")])

    results = []
    total_by_pool = {pool_id: {"earned_fees": 0, "owed_partner_share": 0, "chain": chain}
                     for pool_id, chain in ROCKET_POOL_L2_POOLS.items()}

    print(f"Analyzing Rocket Pool L2 alliance fees owed from {START_DATE.strftime('%Y-%m-%d')}\n")
    print("=" * 100)

    for csv_file in v3_files:
        start_date, end_date = parse_date_from_filename(csv_file.name)
        if start_date is None:
            continue

        if end_date < START_DATE:
            continue

        print(f"\nProcessing: {csv_file.name} (Period: {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')})")

        with open(csv_file, "r") as f:
            reader = csv.DictReader(f)
            period_data = []

            for row in reader:
                pool_id = row["pool_id"].lower()
                if pool_id in ROCKET_POOL_L2_POOLS:
                    earned_fees = float(row["earned_fees"])
                    partner_share = earned_fees * PARTNER_SHARE_PCT
                    chain = row["chain"]
                    symbol = row["symbol"]

                    period_data.append({
                        "pool_id": pool_id,
                        "chain": chain,
                        "symbol": symbol,
                        "earned_fees": earned_fees,
                        "partner_share": partner_share
                    })

                    total_by_pool[pool_id]["earned_fees"] += earned_fees
                    total_by_pool[pool_id]["owed_partner_share"] += partner_share

                    results.append({
                        "period_start": start_date.strftime("%Y-%m-%d"),
                        "period_end": end_date.strftime("%Y-%m-%d"),
                        "pool_id": pool_id,
                        "chain": chain,
                        "symbol": symbol,
                        "earned_fees": earned_fees,
                        "partner_share": partner_share
                    })

            if period_data:
                for data in period_data:
                    print(f"  {data['chain']:10} | {data['symbol']:30} | Earned: ${data['earned_fees']:>10.2f} | Partner Share: ${data['partner_share']:>8.2f}")

    print("\n" + "=" * 100)
    print("\nSUMMARY BY POOL:")
    print("-" * 80)

    grand_total_earned = 0
    grand_total_owed = 0

    for pool_id, data in total_by_pool.items():
        chain = data["chain"]
        earned = data["earned_fees"]
        owed = data["owed_partner_share"]
        grand_total_earned += earned
        grand_total_owed += owed
        print(f"{chain:12} ({pool_id[:10]}...): Earned: ${earned:>12.2f} | Owed Partner Share: ${owed:>10.2f}")

    print("-" * 80)
    print(f"{'GRAND TOTAL':12}                      : Earned: ${grand_total_earned:>12.2f} | Owed Partner Share: ${grand_total_owed:>10.2f}")
    print("\n" + "=" * 100)

    unique_periods = set((r["period_start"], r["period_end"]) for r in results)
    print(f"\nAnalysis covers {len(unique_periods)} bi-weekly periods")
    print(f"Total owed to Rocket Pool for L2 pools: ${grand_total_owed:,.2f}")

    output = {
        "total_usdc": str(grand_total_owed),
        "total_usdc_raw": int(grand_total_owed * 1e6),
        "periods_count": len(unique_periods),
        "pool_totals": {
            pool_id: {
                "pool_id": pool_id,
                "chain": data["chain"],
                "earned_fees": str(data["earned_fees"]),
                "partner_share": str(data["owed_partner_share"]),
            }
            for pool_id, data in total_by_pool.items()
        },
        "periods": results,
    }

    output_file = Path(__file__).parent / "rocket_retroactive_fees.json"
    with open(output_file, "w") as f:
        json.dump(output, f, indent=2)

    print(f"\nReport saved to: {output_file}")

if __name__ == "__main__":
    main()
