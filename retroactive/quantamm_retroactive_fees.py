from decimal import Decimal
from datetime import datetime, timedelta
import logging
import json
import csv
from pathlib import Path
from typing import Dict
from dotenv import load_dotenv
from bal_tools import Subgraph


logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger(__name__)

load_dotenv()

# Per BIP-871: Non-core pools with gauge get 50%, without gauge get 80%
QUANTAMM_POOLS = {
    "mainnet": {
        "0x6b61d8680c4f9e560c8306807908553f95c749c5": 0.50,  # non-core with gauge
        "0xd4ed17bbf48af09b87fd7d8c60970f5da79d4852": 0.80,  # non-core without gauge
    },
    "base": {
        "0xb4161aea25bd6c5c8590ad50deb4ca752532f05d": 0.50,  # non-core with gauge
    }
}

RETROACTIVE_START = datetime(2025, 5, 8)
RETROACTIVE_END = datetime(2025, 8, 28)


class RetroactiveFeeCalculator:
    def __init__(self, end_date: datetime):
        self.end = end_date
        self.start = end_date - timedelta(days=14)
        self.date_range = (int(self.start.timestamp()), int(self.end.timestamp()))
        self.subgraphs = {chain: Subgraph(chain) for chain in ["mainnet", "base"]}

    def calculate_for_period(self) -> Dict:
        date_str = f"{self.start.strftime('%Y-%m-%d')} to {self.end.strftime('%Y-%m-%d')}"
        logger.info(f"\nProcessing {date_str}")

        period_total = Decimal(0)
        pool_details = []

        for chain_name in ["mainnet", "base"]:
            quantamm_pool_data = []
            for pool_id, partner_rate in QUANTAMM_POOLS.get(chain_name, {}).items():
                earned = self.subgraphs[chain_name].get_v3_protocol_fees(pool_id, chain_name, self.date_range)
                if earned > 0:
                    quantamm_pool_data.append({'pool_id': pool_id, 'partner_rate': partner_rate, 'earned_fees': earned})

            quantamm_earned = sum(p['earned_fees'] for p in quantamm_pool_data)
            if not quantamm_earned:
                continue

            csv_path = Path(f"fee_allocator/allocations/noncore/v3_noncore_{date_str.replace(' to ', '_')}.csv")

            with open(csv_path, 'r') as f:
                chain_data = {row['chain']: row for row in csv.DictReader(f)}

            row = chain_data[chain_name]
            core_earned = Decimal(row['total_fees_earned_twap'])
            noncore_earned = Decimal(row['noncore_fees'])
            total_fees_collected = Decimal(row['total_fees_collected'])

            total_fees_earned = core_earned + noncore_earned
            partner_noncore_collected = total_fees_collected * (quantamm_earned / total_fees_earned)

            for pool_data in quantamm_pool_data:
                pool_share = pool_data['earned_fees'] / quantamm_earned
                partner_amount = partner_noncore_collected * pool_share * Decimal(str(pool_data['partner_rate']))

                period_total += partner_amount
                pool_details.append({
                    'pool_id': pool_data['pool_id'],
                    'chain': chain_name,
                    'has_gauge': pool_data['partner_rate'] == 0.50,
                    'earned_fees': str(pool_data['earned_fees']),
                    'pool_share': str(pool_share),
                    'allocated_amount': str(partner_noncore_collected * pool_share),
                    'partner_share_pct': str(pool_data['partner_rate']),
                    'partner_amount': str(partner_amount)
                })

        return {
            'period': date_str,
            'total': str(period_total),
            'pools': pool_details
        }


def calculate_retroactive_fees():
    periods = [RETROACTIVE_START + timedelta(days=14*i) for i in range((RETROACTIVE_END - RETROACTIVE_START).days // 14 + 1)]

    total_retroactive = Decimal(0)
    all_periods = []
    pool_totals = {}

    for period_end in periods:
        result = RetroactiveFeeCalculator(period_end).calculate_for_period()
        all_periods.append(result)
        total_retroactive += Decimal(result['total'])

        for pool_detail in result.get('pools', []):
            pool_key = f"{pool_detail['chain']}_{pool_detail['pool_id']}"
            if pool_key not in pool_totals:
                pool_totals[pool_key] = {
                    'pool_id': pool_detail['pool_id'],
                    'chain': pool_detail['chain'],
                    'has_gauge': pool_detail.get('has_gauge', False),
                    'partner_share_pct': pool_detail.get('partner_share_pct'),
                    'total_partner_amount': Decimal(0)
                }
            pool_totals[pool_key]['total_partner_amount'] += Decimal(pool_detail.get('partner_amount', 0))

    for pool_key in pool_totals:
        pool_totals[pool_key]['total_partner_amount'] = str(pool_totals[pool_key]['total_partner_amount'])

    output = {
        'total_usdc': str(total_retroactive),
        'total_usdc_raw': int(total_retroactive * Decimal('1e6')),
        'periods_count': len(periods),
        'periods': all_periods,
        'pool_totals': pool_totals
    }

    return output


def generate_quantamm_retroactive_report(payload_path=None):
    results = calculate_retroactive_fees()

    amount = Decimal(results['total_usdc'])
    quantamm_multisig = "0xd785201fd2D9be7602F6682296Bb415530C027Ef"

    vebal_injector = "0x8AD2512819A7eae1dd398973EFfaE48dafBe8255"

    vebal_line = None
    quantamm_line = None

    if payload_path and payload_path.exists():
        with open(payload_path) as f:
            payload_data = json.load(f)

        vebal_value = None
        quantamm_value = None

        for tx in payload_data.get('transactions', []):
            if tx.get('contractInputsValues', {}).get('_to') == vebal_injector:
                vebal_value = tx['contractInputsValues']['_value']
            elif tx.get('contractInputsValues', {}).get('_to') == quantamm_multisig:
                quantamm_value = tx['contractInputsValues']['_value']

        if vebal_value or quantamm_value:
            with open(payload_path) as f:
                for i, line in enumerate(f, 1):
                    if vebal_value and f'"{vebal_value}"' in line:
                        vebal_line = i
                    if quantamm_value and f'"{quantamm_value}"' in line:
                        quantamm_line = i

    report_lines = []
    report_lines.append("\n## QuantAMM Retroactive Adjustment (Action Needed)")
    report_lines.append(f"\n**Period:** May 8, 2025 - August 28, 2025")
    report_lines.append("")
    report_lines.append("| Pool | Chain | Type | Partner Share | Amount |")
    report_lines.append("|------|-------|------|---------------|--------|")

    for _, pool_data in results['pool_totals'].items():
        share_pct = int(Decimal(str(pool_data['partner_share_pct'])) * 100)
        pool_type = "Non-core w/ gauge" if pool_data['has_gauge'] else "Non-core no gauge"
        partner_amount = Decimal(pool_data['total_partner_amount'])
        report_lines.append(f"| {pool_data['pool_id'][:8]}... | {pool_data['chain'].capitalize()} | {pool_type} | {share_pct}% | ${partner_amount:,.2f} |")

    report_lines.append(f"| **TOTAL** | | | | **${amount:,.2f}** |")

    report_lines.append("")
    report_lines.append(f"**Action Required:**")

    raw_amount = int(amount * Decimal('1000000'))

    if vebal_line and payload_path:
        report_lines.append(f"1. Subtract ${amount:,.2f} (raw: {raw_amount}) from the veBAL fee ([line {vebal_line}](../payloads/{payload_path.name}#L{vebal_line}))")
    else:
        report_lines.append(f"1. Subtract ${amount:,.2f} (raw: {raw_amount}) from the veBAL fee")

    if quantamm_line and payload_path:
        report_lines.append(f"2. Add ${amount:,.2f} (raw: {raw_amount}) to QuantAMM partner transfer ([line {quantamm_line}](../payloads/{payload_path.name}#L{quantamm_line}))")
    else:
        report_lines.append(f"2. Add ${amount:,.2f} (raw: {raw_amount}) to QuantAMM partner transfer (`{quantamm_multisig}`)")

    output_file = Path(__file__).parent / "quantamm_retroactive_fees.json"
    with open(output_file, 'w') as f:
        json.dump(results, f, indent=2)

    return "\n".join(report_lines), amount