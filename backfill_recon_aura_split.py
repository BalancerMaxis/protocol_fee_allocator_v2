import argparse
import datetime
import json
from decimal import Decimal
from pathlib import Path

import pandas as pd

from fee_allocator.votemarket_analytics import get_aura_share_per_gauge
from fee_allocator.logger import logger

PROJECT_ROOT = Path(__file__).parent
SUMMARIES_DIR = PROJECT_ROOT / "fee_allocator" / "summaries"
INCENTIVES_DIR = PROJECT_ROOT / "fee_allocator" / "allocations" / "incentives"
BRIBES_DIR = PROJECT_ROOT / "fee_allocator" / "allocations" / "output_for_msig"

STAKEDAO_MIGRATION_PERIOD_START = 1767225600


def _ts_to_date_str(ts: int) -> str:
    return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).strftime("%Y-%m-%d")


def _load_gauge_data_from_incentives_csv(csv_path: Path) -> pd.DataFrame:
    df = pd.read_csv(csv_path)
    if "gauge_address" in df.columns and "voting_pool_override" in df.columns:
        return df
    return None


def _load_gauge_data_from_bribe_csv(csv_path: Path) -> pd.DataFrame:
    if not csv_path.exists():
        return None
    df = pd.read_csv(csv_path)
    if "target" not in df.columns:
        return None
    bribe_rows = df[df["platform"].isna()] if "platform" in df.columns else df
    bribe_rows = bribe_rows[bribe_rows["amount"] > 0].copy()
    if bribe_rows.empty:
        return None
    bribe_rows = bribe_rows.rename(columns={"target": "gauge_address", "amount": "total_incentives"})
    if "voting_pool_override" not in bribe_rows.columns:
        bribe_rows["voting_pool_override"] = ""
    bribe_rows["voting_pool_override"] = bribe_rows["voting_pool_override"].fillna("")
    return bribe_rows


def _compute_aura_split(gauge_data: pd.DataFrame, gauge_aura_shares: dict) -> pd.DataFrame:
    aura_list = []
    bal_list = []
    for _, row in gauge_data.iterrows():
        total = Decimal(str(row.get("total_incentives", 0)))
        override = str(row.get("voting_pool_override", "")).strip()
        gauge = str(row.get("gauge_address", "")).strip().lower()

        if override == "aura":
            aura_share = Decimal(1)
        elif override == "bal":
            aura_share = Decimal(0)
        elif gauge:
            aura_share = gauge_aura_shares.get(gauge, Decimal(0))
        else:
            aura_share = Decimal(0)

        aura_list.append(float(round(total * aura_share, 4)))
        bal_list.append(float(round(total - total * aura_share, 4)))

    gauge_data = gauge_data.copy()
    gauge_data["aura_incentives"] = aura_list
    gauge_data["bal_incentives"] = bal_list
    return gauge_data


def _get_total_incentives(entry: dict) -> float:
    if "totalIncentives" in entry:
        return entry["totalIncentives"]
    aura = entry.get("auraIncentives", 0) or 0
    bal = entry.get("balIncentives", 0) or 0
    return aura + bal


def backfill(dry_run: bool = False):
    for version in ["v2", "v3"]:
        recon_path = SUMMARIES_DIR / f"{version}_recon.json"
        if not recon_path.exists():
            logger.info(f"No recon file for {version}, skipping")
            continue

        with open(recon_path) as f:
            data = json.load(f)

        modified = False
        for entry in data:
            if entry["periodStart"] < STAKEDAO_MIGRATION_PERIOD_START:
                continue

            total_incentives = _get_total_incentives(entry)
            aura_incentives = entry.get("auraIncentives", 0) or 0
            bal_incentives = entry.get("balIncentives", 0) or 0

            if (aura_incentives + bal_incentives) != 0:
                continue

            if total_incentives == 0:
                if "auraIncentives" not in entry:
                    entry["auraIncentives"] = 0.0
                    entry["balIncentives"] = 0.0
                    entry["auravebalShare"] = 0
                    entry["auraIncentivesPct"] = 0.0
                    entry["balIncentivesPct"] = 0.0
                    modified = True
                continue

            period_start = entry["periodStart"]
            period_end = entry["periodEnd"]
            start_str = _ts_to_date_str(period_start)
            end_str = _ts_to_date_str(period_end)

            logger.info(f"[{version}] Processing period {start_str} to {end_str}")

            gauge_aura_shares = get_aura_share_per_gauge(period_start, period_end)
            if not gauge_aura_shares:
                logger.info(f"[{version}] No VoteMarket data for {start_str}_{end_str}, skipping")
                continue

            incentives_csv = INCENTIVES_DIR / f"{version}_incentives_{start_str}_{end_str}.csv"
            gauge_data = None

            if incentives_csv.exists():
                gauge_data = _load_gauge_data_from_incentives_csv(incentives_csv)

            if gauge_data is None:
                end_date = _ts_to_date_str(period_end)
                bribe_csv = BRIBES_DIR / f"{version}_bribes_{end_date}.csv"
                gauge_data = _load_gauge_data_from_bribe_csv(bribe_csv)

            if gauge_data is None:
                logger.warning(f"[{version}] No gauge data found for {start_str}_{end_str}, skipping")
                continue

            gauge_data = _compute_aura_split(gauge_data, gauge_aura_shares)

            total_aura = sum(gauge_data["aura_incentives"])
            total_bal = sum(gauge_data["bal_incentives"])

            logger.info(f"[{version}] {start_str}_{end_str}: aura={total_aura:.2f} bal={total_bal:.2f}")

            if not dry_run:
                if incentives_csv.exists():
                    full_df = pd.read_csv(incentives_csv)
                    if "gauge_address" in full_df.columns:
                        updated = _compute_aura_split(full_df, gauge_aura_shares)
                        updated.to_csv(incentives_csv, index=False)
                        logger.info(f"[{version}] Updated incentives CSV: {incentives_csv.name}")

                entry["auraIncentives"] = round(total_aura, 2)
                entry["balIncentives"] = round(total_bal, 2)
                combined = total_aura + total_bal
                entry["auravebalShare"] = round(total_aura / combined, 2) if combined > 0 else 0
                total_distributed = entry.get("totalDistributed", entry.get("incentivesDistributed", 0))
                entry["auraIncentivesPct"] = round(total_aura / total_distributed, 4) if total_distributed > 0 else 0.0
                entry["balIncentivesPct"] = round(total_bal / total_distributed, 4) if total_distributed > 0 else 0.0
                modified = True

        if modified and not dry_run:
            with open(recon_path, "w") as f:
                json.dump(data, f, indent=2)
            logger.info(f"[{version}] Wrote updated recon to {recon_path.name}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill aura/bal split into recon JSON")
    parser.add_argument("--dry_run", action="store_true", help="Print what would be done without writing")
    args = parser.parse_args()
    backfill(dry_run=args.dry_run)
