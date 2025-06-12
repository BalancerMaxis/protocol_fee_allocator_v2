import argparse
import json
from pathlib import Path
from datetime import datetime


def merge_duplicate_transfers(transactions: list) -> list:
    """Merge duplicate transfer and approve transactions to the same address"""
    # Group transfers and approvals by unique key
    transfer_groups = {}
    approval_groups = {}
    other_transactions = []
    
    for tx in transactions:
        method_name = tx.get("contractMethod", {}).get("name")
        
        if method_name == "transfer":
            # Create unique key for transfer transactions
            key = (tx["to"], tx["contractMethod"]["name"], tx["contractInputsValues"]["_to"])
            
            if key in transfer_groups:
                # Add to existing amount
                existing_amount = int(transfer_groups[key]["contractInputsValues"]["_value"])
                new_amount = int(tx["contractInputsValues"]["_value"])
                transfer_groups[key]["contractInputsValues"]["_value"] = str(existing_amount + new_amount)
            else:
                # First occurrence of this transfer
                transfer_groups[key] = tx.copy()
                
        elif method_name == "approve":
            # Create unique key for approval transactions
            key = (tx["to"], tx["contractMethod"]["name"], tx["contractInputsValues"]["_spender"])
            
            if key in approval_groups:
                # For approvals, sum the amounts (same as transfers)
                existing_amount = int(approval_groups[key]["contractInputsValues"]["_value"])
                new_amount = int(tx["contractInputsValues"]["_value"])
                approval_groups[key]["contractInputsValues"]["_value"] = str(existing_amount + new_amount)
            else:
                # First occurrence of this approval
                approval_groups[key] = tx.copy()
        else:
            # Not a transfer or approval, keep as is
            other_transactions.append(tx)
    
    # Combine merged transfers, approvals, and other transactions
    merged_transactions = list(transfer_groups.values()) + list(approval_groups.values()) + other_transactions
    
    return merged_transactions


def combine_payloads_from_paths(v2_payload_path: Path, v3_payload_path: Path, output_path: Path) -> Path:
    """Combine V2 and V3 payloads into a single file with duplicate transfer merging"""
    with open(v2_payload_path) as f:
        v2_payload = json.load(f)
    
    with open(v3_payload_path) as f:
        v3_payload = json.load(f)
    
    # Merge all transactions and then merge duplicates
    all_transactions = v2_payload["transactions"] + v3_payload["transactions"]
    merged_transactions = merge_duplicate_transfers(all_transactions)
    
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
        "transactions": merged_transactions
    }
    
    with open(output_path, "w") as f:
        json.dump(combined_payload, f, indent=2)
    
    print(f"Combined payload written to {output_path}")
    print(f"Original transactions: {len(all_transactions)}")
    print(f"Merged transactions: {len(merged_transactions)}")
    print(f"Duplicate transfers/approvals merged: {len(all_transactions) - len(merged_transactions)}")
    
    return output_path


def combine_payloads(fees_file_name: str) -> None:
    date_str = fees_file_name.split('_')[3].split('.')[0]
    
    payload_dir = Path("fee_allocator/payloads")
    
    v2_file = payload_dir / f"v2_{date_str}.json"
    v3_file = payload_dir / f"v3_{date_str}.json"
    
    if not v2_file.exists() or not v3_file.exists():
        raise FileNotFoundError(f"Could not find both v2 and v3 payload files for date {date_str}")
    
    with open(v2_file) as f:
        v2_payload = json.load(f)
    
    with open(v3_file) as f:
        v3_payload = json.load(f)
    
    # Merge all transactions and then merge duplicates
    all_transactions = v2_payload["transactions"] + v3_payload["transactions"]
    merged_transactions = merge_duplicate_transfers(all_transactions)
    
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
        "transactions": merged_transactions
    }
    
    output_file = payload_dir / f"{date_str}.json"
    with open(output_file, "w") as f:
        json.dump(combined_payload, f, indent=2)
    
    print(f"Combined payload written to {output_file}")
    print(f"Original transactions: {len(all_transactions)}")
    print(f"Merged transactions: {len(merged_transactions)}")
    print(f"Duplicate transfers/approvals merged: {len(all_transactions) - len(merged_transactions)}")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--payload_file_name", help="Fees file name", type=str, required=True)
    args = parser.parse_args()
    combine_payloads(args.payload_file_name)

if __name__ == "__main__":
    main() 