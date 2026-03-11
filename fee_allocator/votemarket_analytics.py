from decimal import Decimal
from typing import Dict, List, Tuple
import requests

from fee_allocator.logger import logger

BALANCER_METADATA_URL = "https://raw.githubusercontent.com/stake-dao/votemarket-analytics/main/analytics/votemarket-analytics/balancer/rounds-metadata.json"
BALANCER_ROUND_URL = "https://raw.githubusercontent.com/stake-dao/votemarket-analytics/main/analytics/votemarket-analytics/balancer/{round_id}.json"
VLAURA_METADATA_URL = "https://raw.githubusercontent.com/stake-dao/votemarket-analytics/main/analytics/votemarket-analytics/vlaura/balancer/rounds-metadata.json"
VLAURA_ROUND_URL = "https://raw.githubusercontent.com/stake-dao/votemarket-analytics/main/analytics/votemarket-analytics/vlaura/balancer/{round_id}.json"


def _fetch_json(url: str) -> dict:
    response = requests.get(url)
    response.raise_for_status()
    return response.json()


def _find_matching_rounds(metadata: list, period_start: int, period_end: int) -> List[int]:
    return [r["id"] for r in metadata if r["endVoting"] > period_start and r["endVoting"] <= period_end]


def _aggregate_votes_per_gauge(round_url_template: str, round_ids: List[int]) -> Dict[str, float]:
    votes = {}
    for rid in round_ids:
        data = _fetch_json(round_url_template.format(round_id=rid))
        for gauge in data["analytics"]:
            addr = gauge["gauge"].lower()
            votes[addr] = votes.get(addr, 0) + gauge["nonBlacklistedVotes"]
    return votes


def get_aura_share_per_gauge(period_start: int, period_end: int) -> Dict[str, Decimal]:
    bal_metadata = _fetch_json(BALANCER_METADATA_URL)
    aura_metadata = _fetch_json(VLAURA_METADATA_URL)

    bal_round_ids = _find_matching_rounds(bal_metadata, period_start, period_end)
    aura_round_ids = _find_matching_rounds(aura_metadata, period_start, period_end)

    if not bal_round_ids and not aura_round_ids:
        logger.info(f"VoteMarket: no rounds found for period {period_start}-{period_end}")
        return {}

    logger.info(f"VoteMarket rounds for period {period_start}-{period_end}: bal={bal_round_ids} aura={aura_round_ids}")

    bal_votes = _aggregate_votes_per_gauge(BALANCER_ROUND_URL, bal_round_ids)
    aura_votes = _aggregate_votes_per_gauge(VLAURA_ROUND_URL, aura_round_ids)

    shares = {}
    all_gauges = set(bal_votes) | set(aura_votes)
    for gauge in all_gauges:
        b = bal_votes.get(gauge, 0)
        a = aura_votes.get(gauge, 0)
        total = b + a
        shares[gauge] = Decimal(str(a / total)) if total > 0 else Decimal(0)

    logger.info(f"VoteMarket per-gauge aura shares: { {g[:14]: float(round(s, 4)) for g, s in shares.items()} }")
    return shares
