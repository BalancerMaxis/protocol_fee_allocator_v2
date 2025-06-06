# Balancer Protocol Fee Allocator v2

## Project Overview

[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/BalancerMaxis/protocol_fee_allocator_v2)



The Protocol Fee Allocator v2 is the official system for distributing protocol fees collected across the Balancer ecosystem on a biweekly basis.

This system processes fee data from multiple chains, calculates fee allocations according to governance-approved parameters, and generates executable Safe transactions for distribution.


## Getting Started

  ### Setup
```bash
# Install dependencies
pip install -r requirements.txt

# Set up environment variables
cp .env.example .env
```

### Environment Variables

Required:
- `DRPC_KEY` - API key from [dRPC](https://drpc.org/) for querying multichain data

Optional (but recommended for deterministic block fetching and reproducible results):
- `GRAPH_API_KEY` - API key for The Graph
- `EXPLORER_API_KEY_MAINNET` - Etherscan API key
- `EXPLORER_API_KEY_ARBITRUM` - Arbiscan API key
- `EXPLORER_API_KEY_POLYGON` - Polygonscan API key
- `EXPLORER_API_KEY_BASE` - Basescan API key
- `EXPLORER_API_KEY_GNOSIS` - Gnosisscan API key

### Running the Allocator

```bash
# Basic run (uses default timestamps - last 2 weeks from odd Thursday, default protocol version v2)
python main.py

# Run with specific timestamps
python main.py --ts_now <end_timestamp> --ts_in_the_past <start_timestamp>

# Run with specific protocol version (v2 or v3)
python main.py --protocol_version v3

# Run with custom fee input and output file names
python main.py --fees_file_name v2_fees_2025-04-24_2025-05-08.json --output_file_name v2_incentives_2025-04-24_2025-05-08.csv

# Full example for v2
python main.py \
  --ts_now 1715270400 \
  --ts_in_the_past 1714060800 \
  --fees_file_name v2_fees_2025-04-24_2025-05-08.json \
  --output_file_name v2_incentives_2025-04-24_2025-05-08.csv \
  --protocol_version v2

# Full example for v3
python main.py \
  --ts_now 1715270400 \
  --ts_in_the_past 1714060800 \
  --fees_file_name v3_fees_2025-04-24_2025-05-08.json \
  --output_file_name v3_incentives_2025-04-24_2025-05-08.csv \
  --protocol_version v3
```

> **Note:** The allocator automatically searches for corresponding fee input files in `fee_allocator/fees_collected/` based on the timestamps provided. If no custom fee file is specified, it constructs the filename using the pattern `{protocol}_fees_{start_date}_{end_date}.json`.

### Combining V2 and V3 Payloads
```bash
# After running both v2 and v3 allocations, combine the payloads
python combine_payloads.py --payload_file_name v2_fees_2025-05-08.json
```

### Testing
```bash
# Install dev dependencies
pip install -r requirements-dev.txt

# Run all tests
python -m pytest -s
```

## Workflow Process

### Biweekly Process

1. **Fee Collection**: Every Thursday at 9am UTC, GitHub Actions triggers fee collection via Mimic API
   - Workflow: `.github/workflows/get_mimic_report.yaml`
   - Creates fee files in `fee_allocator/fees_collected/`

2. **Fee Allocation**: When fee files are merged, allocation is automatically triggered
   - Workflow: `.github/workflows/trigger_fee_collection.yaml`
   - Runs allocation for both v2 and v3
   - Combines payloads into single file

3. **Manual Collection**: Can be triggered manually for specific dates
   - Workflow: `.github/workflows/collect_fees.yaml`
   - Specify end date and protocol version

## Output Files

The allocator generates several output files:

- **Incentive CSVs**: `fee_allocator/allocations/incentives/`
  - `v2_incentives_<start>_<end>.csv`
  - `v3_incentives_<start>_<end>.csv`

- **Bribe CSVs**: `fee_allocator/allocations/output_for_msig/`
  - `v2_bribes_<date>.csv`
  - `v3_bribes_<date>.csv`

- **Non-core pool CSVs**: `fee_allocator/allocations/noncore/`
  - `v2_noncore_<start>_<end>.csv`
  - `v3_noncore_<start>_<end>.csv`

- **Payloads**: `fee_allocator/payloads/`
  - `v2_<date>.json` - Safe transaction payload for v2
  - `v3_<date>.json` - Safe transaction payload for v3
  - `<date>.json` - Combined v2+v3 payload

- **Reconciliation**: `fee_allocator/summaries/`
  - `v2_recon.json` - v2 reconciliation data
  - `v3_recon.json` - v3 reconciliation data


## Project Structure

- `fee_allocator/` - Main package
  - `fee_allocator.py` - Main allocator logic
  - `accounting/` - Defines chain-agnostic configuration and data as well as chain-specific accounting logic
  - `allocations/` - Output CSV files
  - `fees_collected/` - Input fee JSON files from Mimic
  - `payloads/` - Safe transaction payloads
  - `summaries/` - Reconciliation reports
- `tests/` - Test suite
- `main.py` - CLI entry point
- `combine_payloads.py` - Utility to merge v2/v3 payloads

## Branch Strategy

- `biweekly-runs` - Main branch for biweekly fee runs
- `gha-mimic-fees-*` - Auto-created branches for Mimic reports
- `gha-biweekly-fees-*` - Auto-created branches for fee allocations
- `manual-fees-*` - Branches for manual fee collections
