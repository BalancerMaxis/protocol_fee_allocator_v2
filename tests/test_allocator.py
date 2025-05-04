from fee_allocator.fee_allocator import FeeAllocator
from fee_allocator.accounting.chains import CorePoolChain, CorePoolRunConfig
from bal_tools.subgraph import DateRange
from web3 import Web3
from decimal import Decimal
from pathlib import Path



def test_core_pool_chain_initialization(chain: CorePoolChain):
    """Test that CorePoolChain can be initialized with valid parameters"""
    assert isinstance(chain.block_range, tuple)
    assert len(chain.block_range) == 2

def test_core_pool_chain_pool_fee_data(chain: CorePoolChain):
    """Test that CorePoolChain can fetch and process pool fee data and calculate fee distributions"""
    chain.set_pool_fee_data()

    # Verify pool fee data was processed
    assert hasattr(chain, 'pool_fee_data')
    if chain.pool_fee_data:
        assert isinstance(chain.pool_fee_data, list)
        for pool_data in chain.pool_fee_data:
            assert hasattr(pool_data, 'pool_id')
            assert hasattr(pool_data, 'total_earned_fees_usd_twap')
            assert type(pool_data.total_earned_fees_usd_twap) is Decimal

    # Verify fee calculations
    assert type(chain.total_earned_fees_usd_twap) is Decimal
    assert type(chain.noncore_fees_collected) is Decimal
    assert type(chain.noncore_to_dao_usd) is Decimal
    assert type(chain.noncore_to_vebal_usd) is Decimal
    assert type(chain.total_fees_earned) is Decimal

def test_core_pool_chain_cache_handling(chain: CorePoolChain):
    cache_file = chain._cache_file_path()
    assert cache_file.parent == chain.chains.cache_dir
    assert str(chain.chains.date_range[0]) in cache_file.name
    assert str(chain.chains.date_range[1]) in cache_file.name

def test_fee_allocator_initialization(allocator: FeeAllocator):
    assert isinstance(allocator.run_config, CorePoolRunConfig)
    assert isinstance(allocator.book, dict)

def test_fee_allocator_allocation_process(allocator: FeeAllocator):
    allocator.allocate()

    # Verify core pool chains were initialized
    assert hasattr(allocator.run_config, '_chains')
    assert isinstance(allocator.run_config._chains, dict)
    
    # Verify aura vebal share was set
    assert allocator.run_config.aura_vebal_share is not None
    assert isinstance(allocator.run_config.aura_vebal_share, Decimal)
    assert 0 <= allocator.run_config.aura_vebal_share <= 1
