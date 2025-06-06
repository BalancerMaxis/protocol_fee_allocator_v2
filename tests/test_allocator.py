from fee_allocator.fee_allocator import FeeAllocator
from fee_allocator.accounting.chains import CorePoolChain, CorePoolRunConfig
from decimal import Decimal
import pytest



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

@pytest.fixture
def allocated_allocator(allocator: FeeAllocator):
    """Run allocation once and return the allocator with allocated state"""
    allocator.allocate()
    return allocator


def test_overall_dao_vebal_allocation(allocated_allocator: FeeAllocator):
    """Test that overall DAO/veBAL allocation percentages match targets"""
    fee_config = allocated_allocator.run_config.fee_config
    
    total_dao = Decimal(0)
    total_vebal = Decimal(0) 
    total_incentives = Decimal(0)
    
    for chain in allocated_allocator.run_config.all_chains:
        for pool in chain.core_pools:
            total_dao += pool.to_dao_usd
            total_vebal += pool.to_vebal_usd
            total_incentives += pool.total_to_incentives_usd
        
        # Add non-core and alliance allocations
        total_dao += chain.noncore_to_dao_usd + chain.alliance_noncore_to_dao_usd
        total_vebal += chain.noncore_to_vebal_usd + chain.alliance_noncore_to_vebal_usd
    
    total_allocated = total_dao + total_vebal + total_incentives
    
    dao_pct = total_dao / total_allocated
    vebal_pct = total_vebal / total_allocated
    incentives_pct = total_incentives / total_allocated
    
    assert abs(dao_pct - fee_config.dao_share_pct) <= Decimal('0.02'), \
        f"DAO allocation {dao_pct:.4f} not within 2% of target {fee_config.dao_share_pct}"
    
    expected_non_dao = 1 - fee_config.dao_share_pct
    assert abs((vebal_pct + incentives_pct) - expected_non_dao) <= Decimal('0.02'), \
        f"veBAL + incentives {vebal_pct + incentives_pct:.4f} not within 2% of target {expected_non_dao}"


def test_core_pool_allocation(allocated_allocator: FeeAllocator):
    """Test that standard core pool allocations match configured percentages"""
    fee_config = allocated_allocator.run_config.fee_config
    
    total_core_fees = Decimal(0)
    total_core_dao = Decimal(0)
    total_core_vebal = Decimal(0)
    total_core_incentives = Decimal(0)
    
    for chain in allocated_allocator.run_config.all_chains:
        for pool in chain.core_pools:
            # Only include standard core pools (exclude alliance pools with partners)
            if not (hasattr(pool, 'to_partner_usd') and pool.to_partner_usd > 0):
                total_core_fees += pool.total_earned_fees_usd_twap
                total_core_dao += pool.to_dao_usd
                total_core_vebal += pool.to_vebal_usd
                total_core_incentives += pool.total_to_incentives_usd
    
    if total_core_fees > 0:
        core_dao_pct = total_core_dao / total_core_fees
        core_vebal_pct = total_core_vebal / total_core_fees
        core_incentives_pct = total_core_incentives / total_core_fees
        
        assert abs(core_dao_pct - fee_config.dao_share_pct) <= Decimal('0.02'), \
            f"Core pool DAO {core_dao_pct:.4f} not within 2% of target {fee_config.dao_share_pct}"
        assert abs(core_vebal_pct - fee_config.vebal_share_pct) <= Decimal('0.02'), \
            f"Core pool veBAL {core_vebal_pct:.4f} not within 2% of target {fee_config.vebal_share_pct}"
        assert abs(core_incentives_pct - fee_config.vote_incentive_pct) <= Decimal('0.02'), \
            f"Core pool incentives {core_incentives_pct:.4f} not within 2% of target {fee_config.vote_incentive_pct}"


def test_noncore_allocation(allocated_allocator: FeeAllocator):
    """Test that non-core pool allocations match configured percentages"""
    fee_config = allocated_allocator.run_config.fee_config
    
    total_noncore_dao = Decimal(0)
    total_noncore_vebal = Decimal(0)
    
    for chain in allocated_allocator.run_config.all_chains:
        total_noncore_dao += chain.noncore_to_dao_usd
        total_noncore_vebal += chain.noncore_to_vebal_usd
    
    if total_noncore_dao + total_noncore_vebal > 0:
        noncore_dao_pct = total_noncore_dao / (total_noncore_dao + total_noncore_vebal)
        noncore_vebal_pct = total_noncore_vebal / (total_noncore_dao + total_noncore_vebal)
        
        assert abs(noncore_dao_pct - fee_config.noncore_dao_share_pct) <= Decimal('0.02'), \
            f"Non-core DAO {noncore_dao_pct:.4f} not within 2% of target {fee_config.noncore_dao_share_pct}"
        assert abs(noncore_vebal_pct - fee_config.noncore_vebal_share_pct) <= Decimal('0.02'), \
            f"Non-core veBAL {noncore_vebal_pct:.4f} not within 2% of target {fee_config.noncore_vebal_share_pct}"



def test_aura_bal_incentive_split(allocated_allocator: FeeAllocator):
    """Test that incentives are split correctly between Aura and BAL"""
    total_aura = Decimal(0)
    total_bal = Decimal(0)
    
    for chain in allocated_allocator.run_config.all_chains:
        for pool in chain.core_pools:
            total_aura += pool.to_aura_incentives_usd
            total_bal += pool.to_bal_incentives_usd
    
    total_incentives = total_aura + total_bal
    
    if total_incentives > 0:
        actual_aura_share = total_aura / total_incentives
        actual_bal_share = total_bal / total_incentives
        
        # Redistribution of incentives may deviate from the target, so a lenient check is used
        assert actual_aura_share > Decimal('0.1'), \
            f"Aura share {actual_aura_share:.4f} is too low"
        assert actual_bal_share > Decimal('0.1'), \
            f"BAL share {actual_bal_share:.4f} is too low"
        assert abs(actual_aura_share + actual_bal_share - 1) < Decimal('0.001'), \
            "Aura and BAL shares don't add up to 100%"
