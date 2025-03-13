from datetime import datetime, timedelta
from typing import TYPE_CHECKING
import pytz
import requests
from fee_allocator.constants import HH_API_URL
from web3 import Web3
import os
from dotenv import load_dotenv
import json



if TYPE_CHECKING:
    from fee_allocator.accounting.chains import CorePoolChain


load_dotenv()

EXPLORER_URLS = {
    "mainnet": "https://api.etherscan.io/api",
    "arbitrum": "https://api.arbiscan.io/api",
    "polygon": "https://api.polygonscan.com/api",
    "gnosis": "https://api.gnosisscan.io/api",
    "avalanche": "https://api.snowtrace.io/api",
    "base": "https://api.basescan.org/api",
}


def get_last_thursday_odd_week():
    # Use the current UTC date and time
    current_datetime = datetime.utcnow().replace(tzinfo=pytz.utc)

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


def get_hh_aura_target(target: str) -> str:
    response = requests.get(f"{HH_API_URL}/aura")
    options = response.json()["data"]
    for option in options:
        if Web3.to_checksum_address(option["proposal"]) == target:
            return option["proposalHash"]
    return False


def get_block_by_ts(timestamp, chain: "CorePoolChain", before=False):
    try:
        api_key = os.getenv(f"EXPLORER_API_KEY_{chain.name.upper()}")
        explorer_url = EXPLORER_URLS.get(chain.name)
        if not api_key or not explorer_url:
            raise KeyError
    except KeyError:
        return chain.subgraph.get_first_block_after_utc_timestamp(timestamp)

    params = {
        "module": "block",
        "action": "getblocknobytime",
        "timestamp": timestamp,
        "closest": "before" if before else "after",
        "apikey": api_key,
    }

    response = requests.get(explorer_url, params=params)
    try:
        response.raise_for_status()
    except requests.exceptions.HTTPError:
        return chain.subgraph.get_first_block_after_utc_timestamp(timestamp)

    data = response.json()

    if data["status"] == "1" and data["message"] == "OK":
        return int(data["result"])
    else:
        return chain.subgraph.get_first_block_after_utc_timestamp(timestamp)

def fetch_collected_fees(start_date: str, end_date: str, fees_file_name: str = None, protocol_version: str = "v2") -> dict:
    # If fees_file_name is provided, use that directly
    if fees_file_name:
        filename = fees_file_name
    else:
        filename = f"{protocol_version}_fees_{start_date}_{end_date}.json"
    
    local_path = f"fee_allocator/fees_collected/{filename}"
    if os.path.exists(local_path):
        with open(local_path) as f:
            return json.load(f)
            
    v1_fees = f"https://raw.githubusercontent.com/BalancerMaxis/protocol_fee_allocator/main/fee_allocator/fees_collected/{filename}"
    response = requests.get(v1_fees)
    
    if response.status_code == 200:
        print(f"fetched collected fees from: {v1_fees}")
        return response.json()
        
    raise FileNotFoundError(f"Could not find fees file {filename} locally or remotely")