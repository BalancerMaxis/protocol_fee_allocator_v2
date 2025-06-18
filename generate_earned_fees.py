import csv
from datetime import datetime, timedelta
from pathlib import Path
from decimal import Decimal
from fee_allocator.fee_allocator import FeeAllocator


SUPPORTED_CHAINS = ["mainnet", "arbitrum", "polygon", "base", "gnosis", "avalanche"]

OUTPUT_DIR = Path(__file__).parent / "fee_allocator" / "allocations" / "incentives" / "current_fees"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def generate_earned_fees_csv(protocol_version: str, date_range: tuple):
    """
    Generate a incentive csv file with pool_id, chain, symbol, and earned_fees columns.
    """

    fee_allocator = FeeAllocator(
        input_fees={chain: 1000.0 for chain in SUPPORTED_CHAINS},
        date_range=date_range,
        protocol_version=protocol_version
    )
    fee_allocator.run_config.set_core_pool_chains_data()

    headers = ["pool_id", "chain", "symbol", "earned_fees"]
    rows = []
    
    for chain in fee_allocator.run_config.all_chains:
        for pool in chain.pool_fee_data:
            rows.append({
                "pool_id": pool.pool_id,
                "chain": chain.name,
                "symbol": pool.symbol,
                "earned_fees": f"{pool.total_earned_fees_usd_twap:.4f}"
            })
    
    rows.sort(key=lambda x: (x["chain"], x["pool_id"]))

    start, end = datetime.fromtimestamp(date_range[0]), datetime.fromtimestamp(date_range[1])
    date_str = f"{start.strftime('%Y-%m-%d')}_{end.strftime('%Y-%m-%d')}"

    filename = f"{protocol_version}_earned_fees_{date_str}.csv"
    output_path = OUTPUT_DIR / filename
    with open(output_path, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)
    
    print(f"Generated {output_path} with {len(rows)} pools")
    return len(rows)


def main():
    end_date = datetime.now()
    start_date = end_date - timedelta(days=14)
    
    date_range = (int(start_date.timestamp()), int(end_date.timestamp()))
    
    print("Generating earned fees CSVs...")
    print(f"Date range: {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}")
    print("=" * 60)
    
    v2_count = generate_earned_fees_csv("v2", date_range)
    v3_count = generate_earned_fees_csv("v3", date_range)
    
    print("=" * 60)
    print(f"Successfully generated:")
    print(f"  - v2_earned_fees_{start_date.strftime('%Y-%m-%d')}_{end_date.strftime('%Y-%m-%d')}.csv ({v2_count} pools)")
    print(f"  - v3_earned_fees_{start_date.strftime('%Y-%m-%d')}_{end_date.strftime('%Y-%m-%d')}.csv ({v3_count} pools)")
    print(f"\nFiles saved to: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()