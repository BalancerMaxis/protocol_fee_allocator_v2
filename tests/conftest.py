from fee_allocator.fee_allocator import FeeAllocator
from fee_allocator.accounting.chains import CorePoolChain, CorePoolRunConfig

import pytest
import json
from pathlib import Path


@pytest.fixture
def fee_period():
    return (1721260800, 1722470400)


@pytest.fixture
def fee_allocator(fee_period):
    with open("tests/input_fees.json") as f:
        input_fees = json.load(f)

    return FeeAllocator(
        input_fees, fee_period, cache_dir=Path("tests/cache"), use_cache=True
    )
