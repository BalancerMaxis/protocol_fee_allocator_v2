import json
from datetime import datetime, timedelta

import requests


BASE_URL = "https://api.mimic.fi/public/summary/"
V2_ENV_ID = "0xd28bd4e036df02abce84bc34ede2a63abcefa0567ff2d923f01c24633262c7f8"
V3_ENV_ID = "0x414f383b177eddab17d82bdd54047a77ba5c9e658a4d5a23412409eb403e379a"
CHAIN_ID = "1"


def get_report(start_date, end_date, env_id):
    # docs: https://mimic-fi.notion.site/Balancer-API-explanation-1289958dbf4d80beb76ad13462898fee
    response = requests.get(
        f"{BASE_URL}{env_id}",
        params={
            "envId": env_id,
            "chainId": CHAIN_ID,
            "startDate": start_date,
            "endDate": end_date,
        },
    )
    response.raise_for_status()
    # breakpoint()
    report = response.json()["depositors"]
    
    cleaned_report = {
        chain.replace("-v3", "") if chain.endswith("-v3") else chain: int(amount)
        for chain, amount in report.items()
    }
    
    total = sum(cleaned_report.values())
    if total > 0:
        return cleaned_report
    else:
        raise ValueError("Sum of collected fees is not > 0")


if __name__ == "__main__":
    # run this every other thursday after the end of an epoch
    today = datetime.now()

    if bool(int(today.strftime("%V")) % 2):
        # week number is uneven; there should be a new report

        yesterday = today - timedelta(days=1)
        epoch_start = today - timedelta(days=14)

        v2_report = get_report(yesterday.strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d"), V2_ENV_ID)

        with open(
            f"fee_allocator/fees_collected/v2_fees_{epoch_start.strftime('%Y-%m-%d')}_{today.strftime('%Y-%m-%d')}.json",
            "w",
        ) as f:
            json.dump(v2_report, f, indent=2)

        v3_report = get_report(yesterday.strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d"), V3_ENV_ID)

        with open(
            f"fee_allocator/fees_collected/v3_fees_{epoch_start.strftime('%Y-%m-%d')}_{today.strftime('%Y-%m-%d')}.json",
            "w",
        ) as f:
            json.dump(v3_report, f, indent=2)

