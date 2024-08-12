from backtesting.v1_allocations import gather_v1_allocations, V1FeeAllocation
from rich.console import Console
from fee_allocator.fee_allocator import FeeAllocator as V2FeeAllocator
from pathlib import Path

console = Console()


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
