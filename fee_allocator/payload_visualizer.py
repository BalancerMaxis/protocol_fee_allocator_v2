import json
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Any, Optional
import requests

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich import box

from bal_addresses import AddrBook
from fee_allocator.constants import ALLIANCE_CONFIG_URL, PARTNER_CONFIG_URL


class PayloadVisualizer:
    # Transaction group display order
    TRANSACTION_PRIORITY_ORDER = [
        "Vote Incentives",
        "veBAL Transfers",
        "DAO Transfers",
        "Beets Transfers",
        "Alliance Transfers",
        "Partner Transfers",
        "Token Approvals"
    ]
    
    # Transfer group types for method categorization
    TRANSFER_GROUPS = ["veBAL Transfers", "DAO Transfers", "Partner Transfers", "Alliance Transfers", "Beets Transfers", "Unknown Transfers"]
    
    def __init__(self):
        self.console = Console()
        self.book = AddrBook("mainnet").flatbook
        self.alliance_addresses, self.alliance_names, self.partner_addresses, self.partner_names = self._load_fee_share_config()
    
    def _load_fee_share_config(self) -> tuple[List[str], Dict[str, str], List[str], Dict[str, str]]:
        """Load alliance and partner addresses and names from GitHub config"""
        # Load alliance config
        response = requests.get(ALLIANCE_CONFIG_URL, timeout=10)
        response.raise_for_status()
        alliance_config = response.json()

        # Load partner config
        response = requests.get(PARTNER_CONFIG_URL, timeout=10)
        response.raise_for_status()
        partner_config = response.json()

        alliance_addresses = []
        alliance_names = {}
        partner_addresses = []
        partner_names = {}

        # Extract alliance members
        alliance_members = alliance_config.get('alliance_members', [])
        for member in alliance_members:
            if 'multisig_address' in member and 'name' in member:
                addr_lower = member['multisig_address'].lower()
                alliance_addresses.append(addr_lower)
                alliance_names[addr_lower] = member['name']

        # Extract partners from the separate partner config
        partners = partner_config.get('partners', [])
        for partner in partners:
            if 'multisig_address' in partner and 'name' in partner:
                addr_lower = partner['multisig_address'].lower()
                partner_addresses.append(addr_lower)
                partner_names[addr_lower] = partner['name']

        return alliance_addresses, alliance_names, partner_addresses, partner_names
    
    def format_amount(self, amount: str, token: str = "USDC") -> str:
        """Format amount with currency symbol"""
        if token == "BAL":
            return f"{int(amount)/1e18:,.2f} BAL"
        else:
            try:
                decimal_amount = Decimal(amount) / Decimal(1e6)
                return f"${decimal_amount:,.2f}"
            except:
                return str(amount)
    
    def format_address(self, address: str) -> str:
        """Format address with name lookup"""
        if not address:
            return ""
        
        # Check if it's a known address
        for key, value in self.book.items():
            if value and value.lower() == address.lower():
                parts = key.split("/")
                name = parts[-1].replace("_", " ").title()
                return f"{name} ({address[:6]}...{address[-4:]})"
        
        addr_lower = address.lower()
        
        # Check if it's an alliance member
        if addr_lower in self.alliance_names:
            alliance_name = self.alliance_names[addr_lower]
            return f"{alliance_name} ({address[:6]}...{address[-4:]})"
        
        # Check if it's a partner
        if addr_lower in self.partner_names:
            partner_name = self.partner_names[addr_lower]
            return f"{partner_name} ({address[:6]}...{address[-4:]})"
        
        # Special case for Beets Treasury
        if addr_lower == "0xea06e3e20658d2e27dcd1a6d5248fd3667e66e26":
            return f"Beets Treasury ({address[:6]}...{address[-4:]})"
        
        # Unknown address
        return f"{address[:6]}...{address[-4:]}"
    
    def group_transactions(self, transactions: List[Dict]) -> Dict[str, List[Dict]]:
        """Group transactions by type"""
        from collections import defaultdict
        groups = defaultdict(list)
        
        for tx in transactions:
            to_addr = tx.get("to", "").lower()
            method = tx.get("contractMethod", {}).get("name", "")
            
            if method == "createCampaign":
                groups["Vote Incentives"].append(tx)
            elif method == "transfer":
                recipient = tx.get("contractInputsValues", {}).get("_to", "").lower()
                if recipient == self.book.get("maxiKeepers/veBalFeeInjector", "").lower():
                    groups["veBAL Transfers"].append(tx)
                elif recipient == self.book.get("multisigs/balancer_onchain_ltd", "0x16b0056636Fcc85f92C49cD49a24bc519d4A1941").lower():
                    groups["DAO Transfers"].append(tx)
                elif recipient == self.book.get("multisigs/beets_treasury").lower():
                    groups["Beets Transfers"].append(tx)
                elif recipient in self.alliance_addresses:
                    groups["Alliance Transfers"].append(tx)
                elif recipient in self.partner_addresses:
                    groups["Partner Transfers"].append(tx)
                else:
                    # Unknown recipient - add to a separate group instead of crashing
                    groups["Unknown Transfers"].append(tx)
                    print(f"Warning: Unknown transfer recipient: {recipient}. Adding to 'Unknown Transfers' group.")
            elif method == "approve":
                groups["Token Approvals"].append(tx)
            else:
                groups["Unknown Transactions"].append(tx)
                print(f"Warning: Unknown transaction method: {method} to address {to_addr}. Adding to 'Unknown Transactions' group.")
        
        return dict(groups)

    def parse_payload(self, payload_path: Path) -> Dict:
        """Load and parse payload JSON"""
        with open(payload_path) as f:
            return json.load(f)
    
    def _load_fee_files(self, fee_files: List[Path]) -> tuple[Decimal, List[str]]:
        """Helper to load fee files and calculate totals.
        
        Returns:
            Tuple of (total_fees_collected, fee_details_list)
        """
        total_fees_collected = Decimal(0)
        fee_details = []
        
        if fee_files:
            for fee_file in fee_files:
                if fee_file.exists():
                    with open(fee_file) as f:
                        fees_data = json.load(f)
                    file_total = sum(Decimal(str(amount)) for amount in fees_data.values())
                    total_fees_collected += file_total
                    fee_details.append(f"{fee_file.stem}: ${file_total/Decimal(1e6):,.2f}")
        
        return total_fees_collected, fee_details
    
    def load_recon_data(self, payload_path: Path) -> Dict[str, Any]:
        """Load reconciliation data for the payload"""
        from fee_allocator.accounting import PROJECT_ROOT
        
        filename = payload_path.stem
        if filename.startswith("v2_"):
            recon_file = Path(PROJECT_ROOT) / "fee_allocator/summaries/v2_recon.json"
        elif filename.startswith("v3_"):
            recon_file = Path(PROJECT_ROOT) / "fee_allocator/summaries/v3_recon.json"
        else:
            v2_recon_file = Path(PROJECT_ROOT) / "fee_allocator/summaries/v2_recon.json"
            v3_recon_file = Path(PROJECT_ROOT) / "fee_allocator/summaries/v3_recon.json"
            
            v2_data = {}
            v3_data = {}
            
            if v2_recon_file.exists():
                with open(v2_recon_file) as f:
                    v2_recon = json.load(f)
                    if v2_recon:
                        v2_data = v2_recon[-1]  # Get latest entry
            
            if v3_recon_file.exists():
                with open(v3_recon_file) as f:
                    v3_recon = json.load(f)
                    if v3_recon:
                        v3_data = v3_recon[-1]  # Get latest entry
            
            # Merge data
            if v2_data and v3_data:
                return {
                    'coreFees': v2_data.get('coreFees', 0) + v3_data.get('coreFees', 0),
                    'noncoreFees': v2_data.get('noncoreFees', 0) + v3_data.get('noncoreFees', 0),
                    'bribeThreshold': v3_data.get('bribeThreshold') or v2_data.get('bribeThreshold')
                }
            elif v2_data:
                return v2_data
            elif v3_data:
                return v3_data
            else:
                return {}
        
        if recon_file.exists():
            with open(recon_file) as f:
                recon_data = json.load(f)
                return recon_data[-1] if recon_data else {}
        return {}
    
    def calculate_totals(self, groups: Dict[str, List[Dict]]) -> Dict[str, Decimal]:
        """Calculate all totals from grouped transactions"""
        totals = {
            "bribes_usdc": Decimal(0),
            "dao_usdc": Decimal(0),
            "vebal_usdc": Decimal(0),
            "vebal_bal": Decimal(0),
            "partner_usdc": Decimal(0),
            "alliance_usdc": Decimal(0),
            "beets_usdc": Decimal(0),
        }

        for group_name, txs in groups.items():
            for tx in txs:
                if group_name == "Vote Incentives":
                    method = tx.get("contractMethod", {}).get("name", "")
                    if method == "createCampaign":
                        params_str = tx.get("contractInputsValues", {}).get("params", "")
                        if params_str.startswith('['):
                            params = json.loads(params_str)
                        else:
                            import ast
                            params = ast.literal_eval(params_str)
                        token = params[3].lower()
                        if token == self.book.get("tokens/USDC", "").lower():
                            totals["bribes_usdc"] += Decimal(params[6])
                elif group_name == "veBAL Transfers":
                    if tx.get("to", "").lower() == self.book.get("tokens/USDC", "").lower():
                        totals["vebal_usdc"] += Decimal(tx["contractInputsValues"]["_value"])
                    elif tx.get("to", "").lower() == self.book.get("tokens/BAL", "").lower():
                        totals["vebal_bal"] += Decimal(tx["contractInputsValues"]["_value"])
                elif group_name == "DAO Transfers":
                    totals["dao_usdc"] += Decimal(tx["contractInputsValues"]["_value"])
                elif group_name == "Beets Transfers":
                    totals["beets_usdc"] += Decimal(tx["contractInputsValues"]["_value"])
                elif group_name == "Partner Transfers":
                    if tx.get("to", "").lower() == self.book.get("tokens/USDC", "").lower():
                        totals["partner_usdc"] += Decimal(tx["contractInputsValues"]["_value"])
                elif group_name == "Alliance Transfers":
                    if tx.get("to", "").lower() == self.book.get("tokens/USDC", "").lower():
                        totals["alliance_usdc"] += Decimal(tx["contractInputsValues"]["_value"])

        totals["total_usdc"] = totals["bribes_usdc"] + totals["dao_usdc"] + totals["vebal_usdc"] + totals["partner_usdc"] + totals["alliance_usdc"] + totals["beets_usdc"]
        return totals
    
    def extract_transaction_data(self, group_name: str, tx: Dict) -> Dict[str, str]:
        """Extract formatted data from a transaction based on its type"""
        data = {}

        if group_name == "Vote Incentives":
            params_str = tx.get("contractInputsValues", {}).get("params", "")
            if params_str.startswith('['):
                params = json.loads(params_str)
            else:
                import ast
                params = ast.literal_eval(params_str)
            gauge = params[1]
            data["col1"] = f"{gauge[:10]}..." if len(gauge) > 10 else gauge
            data["col2"] = self.format_amount(str(params[6]))
            data["col3"] = self.format_address(params[3])
            data["col4"] = "StakeDAO v2"

        elif group_name in self.TRANSFER_GROUPS:
            data["col1"] = self.format_address(tx.get("contractInputsValues", {}).get("_to", ""))
            amount = tx.get("contractInputsValues", {}).get("_value", "0")
            token_addr = tx.get("to", "")
            
            if token_addr.lower() == self.book.get("tokens/USDC", "").lower():
                data["col2"] = self.format_amount(amount, "USDC")
                data["col3"] = "USDC"
            elif token_addr.lower() == self.book.get("tokens/BAL", "").lower():
                data["col2"] = self.format_amount(amount, "BAL")
                data["col3"] = "BAL"
            else:
                data["col2"] = amount
                data["col3"] = self.format_address(token_addr)
        
        elif group_name == "Token Approvals":
            data["col1"] = self.format_address(tx.get("to", ""))
            data["col2"] = self.format_address(tx.get("contractInputsValues", {}).get("_spender", ""))
            data["col3"] = self.format_amount(tx.get("contractInputsValues", {}).get("_value", "0"))
        
        return data
    
    def get_table_headers(self, group_name: str) -> List[str]:
        """Get table headers based on transaction group"""
        if group_name == "Vote Incentives":
            return ["Gauge", "Amount", "Token", "Platform"]
        elif group_name in self.TRANSFER_GROUPS:
            return ["Recipient", "Amount", "Token"]
        elif group_name == "Token Approvals":
            return ["Token", "Spender", "Amount"]
        return ["Field 1", "Field 2", "Field 3"]
    
    def calculate_core_pool_fees(self, recon_data: Dict = None) -> Decimal:
        """Get core pool fees from reconciliation data.
        """
        if recon_data and 'coreFees' in recon_data:
            return Decimal(str(recon_data['coreFees'])) * Decimal('1e6')
        else:
            # No recon data means we can't determine core fees
            return Decimal('0')
    
    def calculate_allocation_metrics(self, totals: Dict[str, Decimal], total_fees_collected: Decimal, recon_data: Dict = None) -> Dict[str, Any]:
        """Calculate allocation percentages and validation metrics"""
        metrics = {}
        
        if totals['total_usdc'] > 0:
            metrics['bribes_pct'] = (totals['bribes_usdc'] / totals['total_usdc'] * 100).quantize(Decimal('0.01'))
            metrics['dao_pct'] = (totals['dao_usdc'] / totals['total_usdc'] * 100).quantize(Decimal('0.01'))
            metrics['vebal_pct'] = (totals['vebal_usdc'] / totals['total_usdc'] * 100).quantize(Decimal('0.01'))
            metrics['partner_pct'] = (totals['partner_usdc'] / totals['total_usdc'] * 100).quantize(Decimal('0.01'))
            metrics['alliance_pct'] = (totals['alliance_usdc'] / totals['total_usdc'] * 100).quantize(Decimal('0.01'))
            metrics['beets_pct'] = (totals['beets_usdc'] / totals['total_usdc'] * 100).quantize(Decimal('0.01'))
            
            # Calculate core pool fees
            core_pool_fees = self.calculate_core_pool_fees(recon_data)
            
            if core_pool_fees > 0:
                # Calculate what percentage of core pool fees went to incentives
                metrics['vote_incentives_pct_of_core'] = (totals['bribes_usdc'] / core_pool_fees * 100).quantize(Decimal('0.01'))
                metrics['core_fees'] = core_pool_fees
            
            # Allocation efficiency
            if total_fees_collected > 0:
                metrics['allocation_efficiency'] = (totals['total_usdc'] / total_fees_collected * 100).quantize(Decimal('0.01'))
                metrics['discrepancy_usd'] = (total_fees_collected - totals['total_usdc']) / Decimal('1e6')
            elif recon_data:
                # Use recon data to determine total fees
                total_from_recon = Decimal(str(recon_data.get('coreFees', 0) + recon_data.get('noncoreFees', 0))) * Decimal('1e6')
                if total_from_recon > 0:
                    metrics['allocation_efficiency'] = (totals['total_usdc'] / total_from_recon * 100).quantize(Decimal('0.01'))
                    metrics['discrepancy_usd'] = (total_from_recon - totals['total_usdc']) / Decimal('1e6')
        
        return metrics
    
    def generate_markdown_summary(self, payload: Dict, groups: Dict[str, List[Dict]], total_fees_collected: Decimal = Decimal(0), recon_data: Dict = None) -> str:
        """Generate markdown version of the summary"""
        total_txs = len(payload["transactions"])
        totals = self.calculate_totals(groups)
        metrics = self.calculate_allocation_metrics(totals, total_fees_collected, recon_data)
        
        md = []
        md.append("## Payload Summary\n")
        md.append(f"**Total Transactions:** {total_txs}\n")
        
        if totals['bribes_usdc'] > 0:
            md.append(f"**Vote Incentives:** ${totals['bribes_usdc']/Decimal(1e6):,.2f}")
            if 'vote_incentives_pct_of_core' in metrics:
                md.append(f" ({metrics['vote_incentives_pct_of_core']}% of core pool fees)")
            md.append("\n")
        
        md.append(f"**DAO Fees:** ${totals['dao_usdc']/Decimal(1e6):,.2f} ({metrics.get('dao_pct', 0)}% of total)\n")
        md.append(f"**veBAL Fees:** ${totals['vebal_usdc']/Decimal(1e6):,.2f} ({metrics.get('vebal_pct', 0)}% of total)\n")
        
        if totals['beets_usdc'] > 0:
            md.append(f"**Beets Fees:** ${totals['beets_usdc']/Decimal(1e6):,.2f} ({metrics.get('beets_pct', 0)}% of total)\n")
        
        if totals['alliance_usdc'] > 0:
            md.append(f"**Alliance Fees:** ${totals['alliance_usdc']/Decimal(1e6):,.2f} ({metrics.get('alliance_pct', 0)}% of total)\n")
        
        if totals['partner_usdc'] > 0:
            md.append(f"**Partner Fees:** ${totals['partner_usdc']/Decimal(1e6):,.2f} ({metrics.get('partner_pct', 0)}% of total)\n")
        
        md.append(f"\n### 💰 **TOTAL USDC DISTRIBUTED: ${totals['total_usdc']/Decimal(1e6):,.2f}**\n")
        
        if 'allocation_efficiency' in metrics:
            md.append(f"\n**Allocation Efficiency:** {metrics['allocation_efficiency']}% of collected fees")
            if metrics.get('discrepancy_usd', 0) != 0:
                md.append(f"\n**⚠️ Discrepancy:** ${abs(metrics['discrepancy_usd']):,.2f}")
        
        return "\n".join(md)
    
    def generate_markdown_table(self, group_name: str, transactions: List[Dict]) -> str:
        """Generate markdown table for a group of transactions"""
        md = [f"\n### {group_name} ({len(transactions)} transactions)\n"]
        
        headers = self.get_table_headers(group_name)
        md.append(f"| {' | '.join(headers)} |")
        md.append(f"|{' | '.join(['-------'] * len(headers))} |")
        
        for tx in transactions:
            data = self.extract_transaction_data(group_name, tx)
            if len(headers) == 4:
                md.append(f"| {data.get('col1', '')} | {data.get('col2', '')} | {data.get('col3', '')} | {data.get('col4', '')} |")
            else:
                md.append(f"| {data.get('col1', '')} | {data.get('col2', '')} | {data.get('col3', '')} |")
        
        return "\n".join(md)
    
    def export_markdown(self, payload_path: Path, fee_files: List[Path] = None) -> str:
        """Export payload visualization as markdown"""
        payload = self.parse_payload(payload_path)

        total_fees_collected, fee_details_raw = self._load_fee_files(fee_files)
        fee_details = [f"- {detail}" for detail in fee_details_raw]

        groups = self.group_transactions(payload["transactions"])

        md = []
        md.append(f"# Fee Allocator Payload Report")
        md.append(f"\n**File:** {payload_path.name}")
        md.append(f"**Created:** {payload['meta'].get('name', 'Unknown')}")

        if fee_details:
            md.append("\n## Fees Collected")
            md.extend(fee_details)
            md.append(f"**Total:** ${total_fees_collected/Decimal(1e6):,.2f}")

        md.append("")

        recon_data = self.load_recon_data(payload_path)

        md.append(self.generate_markdown_summary(payload, groups, total_fees_collected, recon_data))

        md.append("\n## Transaction Details")

        md.append(f"\n**Bribe Threshold:** ${recon_data['bribeThreshold']}")

        for group_name in self.TRANSACTION_PRIORITY_ORDER:
            if group_name in groups and groups[group_name]:
                md.append(self.generate_markdown_table(group_name, groups[group_name]))

        return "\n".join(md)
    
    def create_summary_panel(self, payload: Dict, groups: Dict[str, List[Dict]], total_fees_collected: Decimal = Decimal(0), recon_data: Dict = None) -> Panel:
        """Create summary statistics panel"""
        total_txs = len(payload["transactions"])
        totals = self.calculate_totals(groups)
        metrics = self.calculate_allocation_metrics(totals, total_fees_collected, recon_data)
        
        lines = [
            "[bold cyan]Transaction Summary[/bold cyan]",
            "",
            f"Total Transactions: [bold]{total_txs}[/bold]",
            ""
        ]
        
        if totals['bribes_usdc'] > 0:
            lines.append("[yellow]USDC Allocations:[/yellow]")

            vote_incentives_line = f"• Vote Incentives: [bold]${totals['bribes_usdc']/Decimal(1e6):,.2f}[/bold]"
            if 'vote_incentives_pct_of_core' in metrics:
                vote_incentives_line += f" ({metrics['vote_incentives_pct_of_core']}% of core pool fees)"
            lines.append(vote_incentives_line)

        lines.append(f"• DAO Fees: [bold]${totals['dao_usdc']/Decimal(1e6):,.2f}[/bold] ({metrics.get('dao_pct', 0)}% of total)")
        lines.append(f"• veBAL Fees: [bold]${totals['vebal_usdc']/Decimal(1e6):,.2f}[/bold] ({metrics.get('vebal_pct', 0)}% of total)")
        
        if totals['beets_usdc'] > 0:
            lines.append(f"• Beets Fees: [bold]${totals['beets_usdc']/Decimal(1e6):,.2f}[/bold] ({metrics.get('beets_pct', 0)}% of total)")
        
        if totals['alliance_usdc'] > 0:
            lines.append(f"• Alliance Fees: [bold]${totals['alliance_usdc']/Decimal(1e6):,.2f}[/bold] ({metrics.get('alliance_pct', 0)}% of total)")
        
        if totals['partner_usdc'] > 0:
            lines.append(f"• Partner Fees: [bold]${totals['partner_usdc']/Decimal(1e6):,.2f}[/bold] ({metrics.get('partner_pct', 0)}% of total)")
        
        lines.append("")
        lines.append("[bold magenta]🔍 Allocation Validation[/bold magenta]")
        lines.append("")
        
        # Fee pool breakdown if we have recon data
        if recon_data and 'coreFees' in recon_data:
            core_fees = Decimal(str(recon_data['coreFees']))
            noncore_fees = Decimal(str(recon_data.get('noncoreFees', 0)))
            total_fees = core_fees + noncore_fees
            
            if total_fees > 0:
                core_pct = (core_fees / total_fees * 100).quantize(Decimal('0.01'))
                noncore_pct = (noncore_fees / total_fees * 100).quantize(Decimal('0.01'))
                lines.append("[dim]Fee Pool Breakdown:[/dim]")
                lines.append(f"• Core pool fees: ${core_fees:,.2f} ({core_pct}%)")
                lines.append(f"• Non-core pool fees: ${noncore_fees:,.2f} ({noncore_pct}%)")
                lines.append("")
        
        if totals['vebal_bal'] > 0:
            lines.append("[dim]veBAL Transfers:[/dim]")
            lines.append(f"• USDC: ${totals['vebal_usdc']/Decimal(1e6):,.2f}")
            lines.append(f"• BAL: {totals['vebal_bal']/Decimal(1e18):,.2f} BAL")
        else:
            lines.append("[dim]veBAL Transfers:[/dim]")
            lines.append(f"• USDC: ${totals['vebal_usdc']/Decimal(1e6):,.2f}")
            lines.append(f"• BAL: 0.00 BAL")
        
        lines.append("")
        lines.append(f"[bold green] TOTAL USDC DISTRIBUTED: ${totals['total_usdc']/Decimal(1e6):,.2f}[/bold green]")
        
        if 'allocation_efficiency' in metrics:
            lines.append("")
            if metrics['allocation_efficiency'] >= 100:
                lines.append(f"[bold green]Allocation Efficiency:[/bold green] {metrics['allocation_efficiency']}% of collected fees")
            else:
                lines.append(f"[bold yellow]Allocation Efficiency:[/bold yellow] {metrics['allocation_efficiency']}% of collected fees")
            
            if metrics.get('discrepancy_usd', 0) != 0:
                lines.append(f"[bold red]Discrepancy:[/bold red] ${metrics['discrepancy_usd']:,.2f}")
        
        return Panel("\n".join(lines), title="📊 Payload Summary", border_style="bright_blue")
    
    def create_transaction_table(self, group_name: str, transactions: List[Dict]) -> Table:
        """Create a table for a group of transactions"""
        # Color scheme based on transaction type
        color_map = {
            "Vote Incentives": "cyan",
            "veBAL Transfers": "magenta",
            "DAO Transfers": "green",
            "Beets Transfers": "cyan",
            "Alliance Transfers": "bright_yellow",
            "Partner Transfers": "yellow",
            "Token Approvals": "dim",
        }
        
        style = color_map.get(group_name, "white")
        
        table = Table(
            title=f"[bold {style}]{group_name}[/bold {style}] ({len(transactions)} transactions)",
            box=box.ROUNDED,
            title_style=f"bold {style}",
            header_style=f"bold {style}",
            border_style=style
        )
        
        headers = self.get_table_headers(group_name)
        if len(headers) == 4:
            styles = ["dim", "bold", "dim", "dim"]
            justifies = ["left", "right", "left", "left"]
        else:
            styles = ["dim", "bold", "dim"] if "Approval" not in group_name else ["dim", "dim", "dim"]
            justifies = ["left", "right", "left"]
        
        for header, style_col, justify in zip(headers, styles, justifies):
            table.add_column(header, style=style_col, justify=justify)
        
        # Add rows
        for tx in transactions:
            data = self.extract_transaction_data(group_name, tx)
            if len(headers) == 4:
                table.add_row(data.get('col1', ''), data.get('col2', ''), data.get('col3', ''), data.get('col4', ''))
            else:
                table.add_row(data.get('col1', ''), data.get('col2', ''), data.get('col3', ''))
        
        return table
    
    def visualize_payload(self, payload_path: Path, fee_files: List[Path] = None):
        """Main visualization function"""
        self.console.clear()

        payload = self.parse_payload(payload_path)
        total_fees_collected, fee_details = self._load_fee_files(fee_files)
        groups = self.group_transactions(payload["transactions"])

        header_text = f"[bold cyan]Fee Allocator Payload Visualization[/bold cyan]\n"
        header_text += f"[dim]File: {payload_path.name}[/dim]\n"
        header_text += f"[dim]Created: {payload['meta'].get('name', 'Unknown')}[/dim]"

        if fee_details:
            header_text += f"\n\n[bold yellow]Fees Collected:[/bold yellow]"
            for detail in fee_details:
                header_text += f"\n• {detail}"
            header_text += f"\n[bold]Total: ${total_fees_collected/Decimal(1e6):,.2f}[/bold]"

        header = Panel(
            header_text,
            box=box.DOUBLE,
            border_style="bright_cyan",
            padding=(1, 2)
        )

        self.console.print(header)
        self.console.print()

        recon_data = self.load_recon_data(payload_path)

        summary = self.create_summary_panel(payload, groups, total_fees_collected, recon_data)
        self.console.print(summary)
        self.console.print()

        for group_name in self.TRANSACTION_PRIORITY_ORDER:
            if group_name in groups and groups[group_name]:
                table = self.create_transaction_table(group_name, groups[group_name])
                self.console.print(table)
                self.console.print()


def visualize_payload(payload_path: Path, fee_files: List[Path] = None):
    """Convenience function to visualize a payload"""
    visualizer = PayloadVisualizer()
    visualizer.visualize_payload(payload_path, fee_files)


def visualize_combined_payload(payload_path: Path, v2_fees_file: Path = None, v3_fees_file: Path = None):
    """Visualize a combined payload with fee comparison"""
    fee_files = []
    if v2_fees_file and v2_fees_file.exists():
        fee_files.append(v2_fees_file)
    if v3_fees_file and v3_fees_file.exists():
        fee_files.append(v3_fees_file)

    visualizer = PayloadVisualizer()
    visualizer.visualize_payload(payload_path, fee_files)


def export_markdown(payload_path: Path, fee_files: List[Path] = None) -> str:
    """Export payload visualization as markdown"""
    visualizer = PayloadVisualizer()
    return visualizer.export_markdown(payload_path, fee_files)


def export_combined_markdown(payload_path: Path, v2_fees_file: Path = None, v3_fees_file: Path = None) -> str:
    """Export combined payload visualization as markdown"""
    fee_files = []
    if v2_fees_file and v2_fees_file.exists():
        fee_files.append(v2_fees_file)
    if v3_fees_file and v3_fees_file.exists():
        fee_files.append(v3_fees_file)

    return export_markdown(payload_path, fee_files)


def save_markdown_report(payload_path: Path, fee_files: List[Path] = None, output_path: Path = None) -> Path:
    """Generate and save payload report to reports directory"""
    from fee_allocator.accounting import PROJECT_ROOT

    markdown_content = export_markdown(payload_path, fee_files)

    if output_path is None:
        date_str = payload_path.stem
        if date_str.startswith(("v2_", "v3_")):
            date_str = date_str[3:]

        reports_dir = Path(PROJECT_ROOT) / "fee_allocator" / "reports"
        reports_dir.mkdir(exist_ok=True, parents=True)
        output_path = reports_dir / f"{date_str}.md"

    output_path.write_text(markdown_content)
    print(f"\nMarkdown report saved to: {output_path}")
    return output_path


def save_combined_report(payload_path: Path, v2_fees_file: Path = None, v3_fees_file: Path = None) -> Path:
    """Generate and save combined payload report to reports directory"""
    fee_files = []
    if v2_fees_file and v2_fees_file.exists():
        fee_files.append(v2_fees_file)
    if v3_fees_file and v3_fees_file.exists():
        fee_files.append(v3_fees_file)

    return save_markdown_report(payload_path, fee_files)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Visualize fee allocator payload")
    parser.add_argument("payload_file", type=Path, help="Path to payload JSON file")
    parser.add_argument("--markdown", action="store_true", help="Export as markdown instead of rich output")
    parser.add_argument("--output", type=Path, help="Output file for markdown export")
    parser.add_argument("--v2_fees", type=Path, help="Path to v2 fees JSON file")
    parser.add_argument("--v3_fees", type=Path, help="Path to v3 fees JSON file")

    args = parser.parse_args()

    if args.markdown:
        markdown_content = export_combined_markdown(args.payload_file, args.v2_fees, args.v3_fees)
        if args.output:
            output_path = args.output
        else:
            reports_dir = Path(__file__).parent / "reports"
            reports_dir.mkdir(exist_ok=True)
            output_path = reports_dir / args.payload_file.with_suffix('.md').name

        output_path.parent.mkdir(exist_ok=True, parents=True)
        output_path.write_text(markdown_content)
        print(f"Markdown exported to {output_path}")
    else:
        visualize_combined_payload(args.payload_file, args.v2_fees, args.v3_fees)
