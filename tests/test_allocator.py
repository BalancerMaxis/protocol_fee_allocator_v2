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
    fee_allocator.redistribute_fees()
    incentives_path = fee_allocator.generate_incentives_csv(Path("tests/output"))

    with open("tests/expected_values.json", "r") as f:
        data = json.load(f)

    expected_values = {
        symbol: ExpectedValues(**values) for symbol, values in data.items()
    }
    df = pd.read_csv(incentives_path)

    for symbol, expected in expected_values.items():
        pool = df[df["symbol"] == symbol].iloc[0]

        assert np.isclose(
            pool.earned_fees, expected.earned_fees, rtol=0.1
        ), f"{symbol}: {pool.earned_fees} != {expected.earned_fees}"
        assert np.isclose(
            pool.fees_to_vebal, expected.fees_to_vebal, rtol=0.1
        ), f"{symbol}: {pool.fees_to_vebal} != {expected.fees_to_vebal}"
        assert np.isclose(
            pool.fees_to_dao, expected.fees_to_dao, rtol=0.1
        ), f"{symbol}: {pool.fees_to_dao} != {expected.fees_to_dao}"
        assert np.isclose(
            pool.total_incentives, expected.total_incentives, rtol=0.1
        ), f"{symbol}: {pool.total_incentives} != {expected.total_incentives}"
        assert np.isclose(
            pool.aura_incentives, expected.aura_incentives, rtol=0.1
        ), f"{symbol}: {pool.aura_incentives} != {expected.aura_incentives}"
        assert np.isclose(
            pool.bal_incentives, expected.bal_incentives, rtol=0.1
        ), f"{symbol}: {pool.bal_incentives} != {expected.bal_incentives}"
