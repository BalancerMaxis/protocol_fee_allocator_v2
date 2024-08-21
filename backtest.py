from backtesting.v1_allocations import gather_v1_allocations, V1FeeAllocation
from rich.console import Console
from fee_allocator.fee_allocator import FeeAllocator as V2FeeAllocator
from pathlib import Path

console = Console()

import os
import pandas as pd


def parse_date(filename):
    start, end = filename.split('_')[-2:]
    return f"{start}_{end.split('.')[0]}"

def process_csv(file_path, is_v1):
    df = pd.read_csv(file_path, index_col=0 if is_v1 else None)
    if not is_v1:
        df.set_index('pool_id', inplace=True)
    return df

def calculate_differences(v1_row, v2_row):
    delta_columns = ['earned_fees', 'fees_to_vebal', 'fees_to_dao', 'total_incentives', 'aura_incentives', 'bal_incentives']
    v1_sum = v1_row[delta_columns].sum() if v1_row is not None else 0
    v2_sum = v2_row[delta_columns].sum() if v2_row is not None else 0
    absolute_delta = v2_sum - v1_sum
    
    if v1_sum == v2_sum == 0:
        relative_pct = 0
    elif v1_sum == 0:
        relative_pct = float('inf') if v2_sum > 0 else float('-inf')
    else:
        relative_pct = (v2_sum - v1_sum) / v1_sum * 100
    
    return absolute_delta, relative_pct

def format_difference(abs_diff, pct_diff):
    if pd.isna(abs_diff) or pd.isna(pct_diff):
        return ""
    elif abs_diff == 0 and pct_diff == 0:
        return "0"
    elif pct_diff in [float('inf'), float('-inf')]:
        return f"{abs_diff:.2f} ({pct_diff})"
    else:
        return f"{abs_diff:.2f} ({pct_diff:.2f}%)"

def get_all_pools_info(v1_dir):
    all_pools_info = pd.DataFrame(columns=['chain', 'symbol'])
    for v1_file in os.listdir(v1_dir):
        if v1_file.startswith('incentives_'):
            v1_df = process_csv(os.path.join(v1_dir, v1_file), is_v1=True)
            all_pools_info = all_pools_info.combine_first(v1_df[['chain', 'symbol']])
    return all_pools_info

def process_file_pair(v1_file, v2_file, v1_dir, v2_dir, all_pools_info):
    date_str = parse_date(v1_file)
    v1_df = process_csv(os.path.join(v1_dir, v1_file), is_v1=True)
    v2_df = process_csv(os.path.join(v2_dir, v2_file), is_v1=False)

    results = pd.DataFrame(index=all_pools_info.index)
    results['chain'] = all_pools_info['chain']
    results['symbol'] = all_pools_info['symbol']

    differences = []
    for pool in all_pools_info.index:
        v1_row = v1_df.loc[pool] if pool in v1_df.index else None
        v2_row = v2_df.loc[pool] if pool in v2_df.index else None
        abs_delta, rel_pct = calculate_differences(v1_row, v2_row)
        differences.append(format_difference(abs_delta, rel_pct))

    results[date_str] = differences

    return results

def create_comparison_csv(v1_dir, v2_dir, output_file):
    v1_files = sorted(f for f in os.listdir(v1_dir) if f.startswith('incentives_'))
    v2_files = sorted(f for f in os.listdir(v2_dir) if f.startswith('incentives_'))

    all_pools_info = get_all_pools_info(v1_dir)
    
    all_results = [process_file_pair(v1_f, v2_f, v1_dir, v2_dir, all_pools_info) for v1_f, v2_f in zip(v1_files, v2_files)]
    combined_results = pd.concat(all_results, axis=1)
    combined_results = combined_results.loc[:, ~combined_results.columns.duplicated()]

    date_columns = [col for col in combined_results.columns if col not in ['chain', 'symbol']]
    column_order = ['chain', 'symbol'] + sorted(date_columns)
    combined_results = combined_results[column_order]

    combined_results.to_csv(output_file)
    
    print(f"Comparison results written to {output_file}")


if __name__ == "__main__":
    v1_allocations = gather_v1_allocations()

    for v1_allocation in v1_allocations:
        console.print(
            f"running allocation {v1_allocation.start_date} - {v1_allocation.end_date}"
        )
        v2_allocator = V2FeeAllocator(
            v1_allocation.input_fees,
            (v1_allocation.start_ts, v1_allocation.end_ts),
            cache_dir=Path("backtesting/v2_allocations/cache"),
        )
        v2_allocator.redistribute_fees()
        v2_allocator.generate_incentives_csv(
            Path("backtesting/v2_allocations/allocations")
        )
        
    create_comparison_csv("backtesting/v1_allocations/allocations", "backtesting/v2_allocations/allocations", "allocation_comparison.csv")
