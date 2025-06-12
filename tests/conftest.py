from fee_allocator.fee_allocator import FeeAllocator
from fee_allocator.accounting.chains import CorePoolRunConfig, CorePoolChain

import pytest
from pathlib import Path
from datetime import datetime
from decimal import Decimal
import os

from bal_tools import Web3RpcByChain


@pytest.fixture
def fee_period():
    # past two weeks
    end_time = int(datetime.now().timestamp()) - (12 * 3600)
    start_time = end_time - (14 * 24 * 3600)
    return (start_time, end_time)


@pytest.fixture
def web3():
    return Web3RpcByChain(os.environ["DRPC_KEY"])["mainnet"]


@pytest.fixture
def run_config(fee_period):
    """Fixture to create a CorePoolRunConfig instance"""
    input_fees = {"mainnet": Decimal("1000.0")}
    return CorePoolRunConfig(
        input_fees=input_fees,
        date_range=fee_period,
        cache_dir=Path("tests/cache"),
        use_cache=True
    )

@pytest.fixture
def chain(run_config, web3):
    chain_name = "mainnet"
    fees = 1000
    
    return CorePoolChain(run_config, chain_name, fees, web3)


@pytest.fixture
def allocator(fee_period):
    input_fees = {"mainnet": Decimal("10000000.0")}

    return FeeAllocator(
        input_fees, 
        fee_period, 
        cache_dir=Path("tests/cache"), 
        use_cache=True
    )
