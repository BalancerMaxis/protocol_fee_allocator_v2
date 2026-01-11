from datetime import datetime, timedelta
from typing import Tuple, Optional
import pytz
import os
from dotenv import load_dotenv
import json
import requests
import statistics
import math

from fee_allocator.constants import SNAPSHOT_URL, STAKEDAO_ANALYTICS_BASE_URL


load_dotenv()


def get_aura_max_votes_from_snapshot(n_rounds: int = 2) -> int:
    query = """
    query Proposals($space: String!, $first: Int!) {
        proposals(
            first: $first,
            skip: 0,
            where: { space: $space, title_contains: "Gauge Weight" },
            orderBy: "created",
            orderDirection: desc
        ) {
            id
            title
            scores_total
            created
        }
    }
    """
    response = requests.post(
        SNAPSHOT_URL,
        json={"query": query, "variables": {"space": "gauges.aurafinance.eth", "first": n_rounds}},
        timeout=30
    )
    response.raise_for_status()
    data = response.json()

    proposals = data.get("data", {}).get("proposals", [])
    if not proposals:
        raise ValueError("No Aura gauge weight proposals found on Snapshot")

    return int(max(p["scores_total"] for p in proposals if p.get("scores_total")))


def get_stakedao_cpv_from_analytics(n_rounds: int = 2) -> float:
    metadata_url = f"{STAKEDAO_ANALYTICS_BASE_URL}/rounds-metadata.json"
    response = requests.get(metadata_url, timeout=30)
    response.raise_for_status()
    rounds = response.json()

    latest_rounds = sorted(rounds, key=lambda x: x["id"], reverse=True)[:n_rounds]

    cpv_values = []
    for round_info in latest_rounds:
        round_url = f"{STAKEDAO_ANALYTICS_BASE_URL}/{round_info['id']}.json"
        response = requests.get(round_url, timeout=30)
        response.raise_for_status()
        data = response.json()

        cpv = data.get("globalAverageDollarPerVote")
        if cpv and cpv > 0:
            cpv_values.append(float(cpv))

    if not cpv_values:
        raise ValueError("No valid CPV data found in StakeDAO analytics")

    return statistics.mean(cpv_values)


def calculate_dynamic_min_incentive(n_rounds: int = 2, buffer_pct: float = 0.5) -> int:
    max_votes = get_aura_max_votes_from_snapshot(n_rounds)
    avg_cpv = get_stakedao_cpv_from_analytics(n_rounds)

    min_incentive = math.ceil(max_votes * 0.001 * (1 + buffer_pct) * avg_cpv / 10) * 10
    return int(min_incentive)


def get_last_thursday_odd_week():
    # Use the current UTC date and time
    current_datetime = datetime.now(pytz.UTC)

    # Calculate the difference between the current weekday and Thursday (where Monday is 0 and Sunday is 6)
    days_since_thursday = (current_datetime.weekday() - 3) % 7

    # Calculate the date of the most recent Thursday
    most_recent_thursday = current_datetime - timedelta(days=days_since_thursday)

    # Check if the week of the most recent Thursday is odd
    is_odd_week = most_recent_thursday.isocalendar()[1] % 2 == 1

    # If it's not an odd week or we are exactly on Thursday but need to check if the week before was odd
    if not is_odd_week or (
        days_since_thursday == 0
        and (most_recent_thursday - timedelta(weeks=1)).isocalendar()[1] % 2 == 1
    ):
        # Go back one more week if it's not an odd week
        most_recent_thursday -= timedelta(weeks=1)

    # Ensure the Thursday chosen is in an odd week
    if most_recent_thursday.isocalendar()[1] % 2 == 0:
        most_recent_thursday -= timedelta(weeks=1)

    # Calculate the timestamp of the last Thursday at 00:00 UTC
    last_thursday_odd_utc = most_recent_thursday.replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    return last_thursday_odd_utc


def fetch_collected_fees(start_date: str, end_date: str, fees_file_name: str = None, protocol_version: str = "v2") -> dict:
    # If fees_file_name is provided, use that directly, else derive it from the start and end date
    if fees_file_name:
        filename = fees_file_name
    else:
        filename = f"{protocol_version}_fees_{start_date}_{end_date}.json"
    
    local_path = f"fee_allocator/fees_collected/{filename}"
    if os.path.exists(local_path):
        with open(local_path) as f:
            return json.load(f)
        
    raise FileNotFoundError(f"could not find input fees file at {local_path}")


def parse_date_inputs(
    date_range_string: Optional[str] = None, 
    ts_now: Optional[int] = None, 
    ts_in_the_past: Optional[int] = None
) -> Tuple[int, int, str, str]:
    now = datetime.now(pytz.UTC)
    DELTA = 6000
    default_ts_now = int(now.timestamp()) - DELTA
    default_ts_past = int(get_last_thursday_odd_week().timestamp())
    
    if date_range_string:
        try:
            start_date_str, end_date_str = date_range_string.split('_')
            # Parse dates to ensure they're valid
            start_dt = datetime.strptime(start_date_str, "%Y-%m-%d").replace(tzinfo=pytz.UTC)
            end_dt = datetime.strptime(end_date_str, "%Y-%m-%d").replace(tzinfo=pytz.UTC)
            
            # Convert to timestamps
            ts_in_the_past = int(start_dt.timestamp())
            ts_now = int(end_dt.timestamp())
            
            return ts_in_the_past, ts_now, start_date_str, end_date_str
        except ValueError:
            raise ValueError(f"Invalid date_range_string format. Expected YYYY-MM-DD_YYYY-MM-DD, got: {date_range_string}")
    else:
        # Use timestamps if provided, otherwise use defaults
        ts_now = ts_now or default_ts_now
        ts_in_the_past = ts_in_the_past or default_ts_past
        
        start_date = datetime.fromtimestamp(ts_in_the_past, tz=pytz.UTC).strftime("%Y-%m-%d")
        end_date = datetime.fromtimestamp(ts_now, tz=pytz.UTC).strftime("%Y-%m-%d")
        
        return ts_in_the_past, ts_now, start_date, end_date

