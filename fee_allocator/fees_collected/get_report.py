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
    data = response.json()

    usdc_address = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
    withdraws = data.get("withdraws", {})
    if usdc_address not in withdraws:
        print(f"No USDC withdrawals found for env {env_id}, returning empty report")
        return {}

    usdc = withdraws[usdc_address]
    total_net = int(usdc["net"])
    total_gross_usdc = int(usdc["total"])

    if total_net <= 0:
        print(f"No net fees for env {env_id}, returning empty report")
        return {}

    depositors = {
        k.replace("-v3", ""): int(v)
        for k, v in data["depositors"].items()
        if int(v) > 0
    }
    total_gross = sum(depositors.values())

    if total_gross != total_gross_usdc:
        raise ValueError(
            f"Depositors sum {total_gross} doesn't match USDC total {total_gross_usdc}"
        )

    # calc each chain's share of net amount based on its proportion of gross fees
    report = {
        chain: int(total_net * amount / total_gross)
        for chain, amount in depositors.items()
    }

    # Adjust rounding difference on the largest chain
    rounding_diff = total_net - sum(report.values())
    if rounding_diff:
        largest_chain = max(report, key=report.get)
        report[largest_chain] += rounding_diff

    if sum(report.values()) != total_net:
        raise ValueError(f"Total mismatch: {sum(report.values())} != {total_net}")

    return report


if __name__ == "__main__":
    # run this every other thursday after the end of an epoch
    today = datetime.now()

    if bool(int(today.strftime("%V")) % 2):
        # week number is uneven; there should be a new report

        epoch_start = today - timedelta(days=14)

        v2_report = get_report(
            epoch_start.strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d"), V2_ENV_ID
        )

        with open(
            f"fee_allocator/fees_collected/v2_fees_{epoch_start.strftime('%Y-%m-%d')}_{today.strftime('%Y-%m-%d')}.json",
            "w",
        ) as f:
            json.dump(v2_report, f, indent=2)

        v3_report = get_report(
            epoch_start.strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d"), V3_ENV_ID
        )

        with open(
            f"fee_allocator/fees_collected/v3_fees_{epoch_start.strftime('%Y-%m-%d')}_{today.strftime('%Y-%m-%d')}.json",
            "w",
        ) as f:
            json.dump(v3_report, f, indent=2)

        if not v2_report and not v3_report:
            raise ValueError(
                "Both v2 and v3 reports are empty — no fees collected at all"
            )
