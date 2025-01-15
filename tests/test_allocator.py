from fee_allocator.fee_allocator import FeeAllocator

from pathlib import Path
import pandas as pd
import pytest
from decimal import Decimal
import numpy as np
from dataclasses import dataclass
import json


@dataclass
class ExpectedValues:
    earned_fees: Decimal
    fees_to_vebal: Decimal
    fees_to_dao: Decimal
    total_incentives: Decimal
    aura_incentives: Decimal
    bal_incentives: Decimal


def test_fee_allocator(fee_allocator):
    fee_allocator.run_config.set_core_pool_chains_data()
    fee_allocator.run_config.set_aura_vebal_share()
    fee_allocator.run_config.set_initial_pool_allocation()
    fee_allocator.redistribute_fees()
    incentives_path = fee_allocator.generate_incentives_csv(Path("tests/output"))

    generated_df = pd.read_csv(incentives_path)
    expected_df = pd.read_csv(Path("tests/expected_incentives.csv"))

    assert set(generated_df['pool_id']) == set(expected_df['pool_id']), "Pool IDs don't match between generated and expected results"

    merged_df = pd.merge(generated_df, expected_df, on='pool_id', suffixes=('_gen', '_exp'))
    
    numeric_columns = ['earned_fees', 'fees_to_vebal', 'fees_to_dao', 
                      'total_incentives', 'aura_incentives', 'bal_incentives', 'redirected_incentives', 'reroute_incentives']

    for col in numeric_columns:
        gen_col = f'{col}_gen'
        exp_col = f'{col}_exp'
        
        diff_pct = abs((merged_df[gen_col] - merged_df[exp_col]) / merged_df[exp_col] * 100)

        problems = merged_df[diff_pct > 1]
        
        if not problems.empty:
            error_msg = f"\nValues for {col} differ by more than 1% for the following pools:\n"
            for _, row in problems.iterrows():
                error_msg += f"Pool {row['pool_id']}: Generated={row[gen_col]:.2f}, Expected={row[exp_col]:.2f}, Diff={diff_pct.loc[_]:.2f}%\n"
            pytest.fail(error_msg)

    

