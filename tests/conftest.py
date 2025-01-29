from fee_allocator.fee_allocator import FeeAllocator

import pytest
import json
from pathlib import Path


@pytest.fixture
def fee_period():
    return (1737007200, 1738130400)

@pytest.fixture
def core_pool_list():
    with open("tests/test_data/static_core_pools.json") as f:
        return json.load(f)

@pytest.fixture
def fee_allocator(fee_period, core_pool_list):
    with open("tests/test_data/input_fees.json") as f:
        input_fees = json.load(f)

    return FeeAllocator(
        input_fees, 
        fee_period, 
        cache_dir=Path("tests/cache"), 
        use_cache=True,
        core_pools=core_pool_list
    )
