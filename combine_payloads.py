import argparse
import json
from pathlib import Path
from datetime import datetime


def combine_payloads(fees_file_name: str) -> None:
    # Extract the end date from the fees file name (fees_START-DATE_END-DATE.json)
    date_str = fees_file_name.split('_')[2].split('.')[0]
    
    payload_dir = Path("fee_allocator/payloads")
    
    v2_file = payload_dir / f"v2_{date_str}.json"
    v3_file = payload_dir / f"v3_{date_str}.json"
    
    if not v2_file.exists() or not v3_file.exists():
        raise FileNotFoundError(f"Could not find both v2 and v3 payload files for date {date_str}")
    
    with open(v2_file) as f:
        v2_payload = json.load(f)
    
    with open(v3_file) as f:
        v3_payload = json.load(f)
    
    combined_payload = {
        "version": "1.0",
        "chainId": "1",
        "createdAt": int(datetime.now().timestamp()),
        "meta": {
            "name": "Combined V2+V3 Transactions Batch",
            "description": "",
            "txBuilderVersion": "1.16.3",
            "createdFromSafeAddress": v2_payload["meta"]["createdFromSafeAddress"],
            "createdFromOwnerAddress": "",
            "checksum": "0x0000000000000000000000000000000000000000000000000000000000000000"
        },
        "transactions": v2_payload["transactions"] + v3_payload["transactions"]
    }
    
    output_file = payload_dir / f"{date_str}.json"
    with open(output_file, "w") as f:
        json.dump(combined_payload, f, indent=2)
    
    print(f"Combined payload written to {output_file}")
    print(f"Total transactions: {len(combined_payload['transactions'])}")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--payload_file_name", help="Fees file name", type=str, required=True)
    args = parser.parse_args()
    combine_payloads(args.payload_file_name)

if __name__ == "__main__":
    main() 