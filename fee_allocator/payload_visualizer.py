import json
from pathlib import Path
from decimal import Decimal
from typing import Dict, List, Any
from collections import defaultdict

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich import box

from bal_addresses import AddrBook


class PayloadVisualizer:
    """Visualize fee allocator payload with rich formatting"""
    
    def __init__(self):
        self.console = Console()
        self.book = AddrBook("mainnet").flatbook

        # Known addresses for better display
        self.known_addresses = {
            self.book.get("maxiKeepers/veBalFeeInjector", "").lower(): "veBAL Fee Injector",
            self.book.get("multisigs/dao", "0x10A19e7eE7d7F8a52822f6817de8ea18204F2e4f").lower(): "DAO Multisig",
            self.book.get("hidden_hand2/aura_briber", "").lower(): "Aura Bribe Market",
            self.book.get("hidden_hand2/balancer_briber", "").lower(): "Balancer Bribe Market",
            self.book.get("hidden_hand2/bribe_vault", "").lower(): "Bribe Vault",
            self.book.get("tokens/USDC", "").lower(): "USDC",
            self.book.get("tokens/BAL", "").lower(): "BAL",
        }
    
    def format_address(self, address: str) -> str:
        """Format address with known name if available"""
        addr_lower = address.lower()
        if addr_lower in self.known_addresses:
            return f"{self.known_addresses[addr_lower]} ({address[:6]}...{address[-4:]})"
        return f"{address[:6]}...{address[-4:]}"
    
    def format_amount(self, amount: str, token: str = "USDC") -> str:
        """Format token amounts with proper decimals"""
        try:
            if token == "USDC":
                value = Decimal(amount) / Decimal(1e6)
                return f"${value:,.2f}"
            elif token == "BAL":
                value = Decimal(amount) / Decimal(1e18)
                return f"{value:,.2f} BAL"
            else:
                return amount
        except:
            return amount
    
    def parse_payload(self, payload_path: Path) -> Dict[str, Any]:
        """Load and parse payload JSON"""
        with open(payload_path) as f:
            return json.load(f)
    
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
                    'noncoreFees': v2_data.get('noncoreFees', 0) + v3_data.get('noncoreFees', 0)
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
                if recon_data:
                    return recon_data[-1]  # Return latest entry
        
        return {}
    
    def group_transactions(self, transactions: List[Dict]) -> Dict[str, List[Dict]]:
        """Group transactions by type"""
        groups = defaultdict(list)
        
        for tx in transactions:
            method = tx.get("contractMethod", {}).get("name", "unknown")
            to_addr = tx.get("to", "").lower()
            
            # Categorize transactions
            if method == "approve":
                groups["Token Approvals"].append(tx)
            elif method == "depositBribe":
                if to_addr == self.book.get("hidden_hand2/aura_briber", "").lower():
                    groups["Aura Bribes"].append(tx)
                elif to_addr == self.book.get("hidden_hand2/balancer_briber", "").lower():
                    groups["Balancer Bribes"].append(tx)
                else:
                    groups["Other Bribes"].append(tx)
            elif method == "transfer":
                recipient = tx.get("contractInputsValues", {}).get("_to", "").lower()
                if recipient == self.book.get("maxiKeepers/veBalFeeInjector", "").lower():
                    groups["veBAL Transfers"].append(tx)
                elif recipient == self.book.get("multisigs/dao", "0x10A19e7eE7d7F8a52822f6817de8ea18204F2e4f").lower():
                    groups["DAO Transfers"].append(tx)
                elif recipient == self.book.get("multisigs/beets_treasury").lower():
                    groups["Beets Transfers"].append(tx)
                else:
                    # Check if it's a partner transfer
                    groups["Partner Transfers"].append(tx)
            else:
                groups["Other Transactions"].append(tx)
        
        return dict(groups)

    def calculate_totals(self, groups: Dict[str, List[Dict]]) -> Dict[str, Decimal]:
        """Calculate all totals from grouped transactions"""
        totals = {
            "bribes_usdc": Decimal(0),
            "aura_bribes_usdc": Decimal(0),
            "bal_bribes_usdc": Decimal(0),
            "dao_usdc": Decimal(0),
            "vebal_usdc": Decimal(0),
            "vebal_bal": Decimal(0),
            "partner_usdc": Decimal(0),
            "beets_usdc": Decimal(0),
        }
        
        for group_name, txs in groups.items():
            for tx in txs:
                if "Bribe" in group_name:
                    if tx.get("contractInputsValues", {}).get("_token", "").lower() == self.book.get("tokens/USDC", "").lower():
                        amount = Decimal(tx["contractInputsValues"]["_amount"])
                        totals["bribes_usdc"] += amount
                        if group_name == "Aura Bribes":
                            totals["aura_bribes_usdc"] += amount
                        elif group_name == "Balancer Bribes":
                            totals["bal_bribes_usdc"] += amount
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

        totals["total_usdc"] = totals["bribes_usdc"] + totals["dao_usdc"] + totals["vebal_usdc"] + totals["partner_usdc"] + totals["beets_usdc"]
        return totals
    
    def extract_transaction_data(self, group_name: str, tx: Dict) -> Dict[str, str]:
        """Extract formatted data from a transaction based on its type"""
        data = {}
        
        if "Bribe" in group_name:
            data["col1"] = tx.get("contractInputsValues", {}).get("_proposal", "")[:10] + "..."
            data["col2"] = self.format_amount(tx.get("contractInputsValues", {}).get("_amount", "0"))
            data["col3"] = self.format_address(tx.get("contractInputsValues", {}).get("_token", ""))
        
        elif group_name in ["veBAL Transfers", "DAO Transfers", "Partner Transfers", "Beets Transfers"]:
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
        if "Bribe" in group_name:
            return ["Gauge/Proposal", "Amount", "Token"]
        elif group_name in ["veBAL Transfers", "DAO Transfers", "Partner Transfers", "Beets Transfers"]:
            return ["Recipient", "Amount", "Token"]
        elif group_name == "Token Approvals":
            return ["Token", "Spender", "Amount"]
        return ["Field 1", "Field 2", "Field 3"]
    
    def calculate_core_pool_fees(self, recon_data: Dict = None) -> Decimal:
        """Get core pool fees from reconciliation data.
        """
        if recon_data and 'coreFees' in recon_data:
            # Use actual core fees from reconciliation data
            return Decimal(str(recon_data['coreFees'])) * Decimal('1e6')  # Convert to raw USDC units
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
            metrics['beets_pct'] = (totals['beets_usdc'] / totals['total_usdc'] * 100).quantize(Decimal('0.01'))
            
            # Calculate core pool fees
            core_pool_fees = self.calculate_core_pool_fees(recon_data)
            
            if core_pool_fees > 0:
                # Calculate what percentage of core pool fees went to incentives
                metrics['vote_incentives_pct_of_core'] = (totals['bribes_usdc'] / core_pool_fees * 100).quantize(Decimal('0.01'))
                metrics['core_fees'] = core_pool_fees
                
                # Calculate non-core fees
                if recon_data and 'noncoreFees' in recon_data:
                    metrics['noncore_fees'] = Decimal(str(recon_data['noncoreFees'])) * Decimal('1e6')
                elif total_fees_collected > 0:
                    metrics['noncore_fees'] = total_fees_collected - core_pool_fees
                else:
                    # Fallback from distributed amounts
                    metrics['noncore_fees'] = totals['total_usdc'] - core_pool_fees
                
                metrics['core_pool_pct'] = (core_pool_fees / (core_pool_fees + metrics['noncore_fees']) * 100).quantize(Decimal('0.01'))
            else:
                metrics['vote_incentives_pct_of_core'] = Decimal('0')
                metrics['core_fees'] = Decimal('0')
                metrics['noncore_fees'] = total_fees_collected if total_fees_collected > 0 else totals['total_usdc']
                metrics['core_pool_pct'] = Decimal('0')
            
            if totals['bribes_usdc'] > 0:
                metrics['aura_bribe_pct'] = (totals['aura_bribes_usdc'] / totals['bribes_usdc'] * 100).quantize(Decimal('0.01'))
                metrics['bal_bribe_pct'] = (totals['bal_bribes_usdc'] / totals['bribes_usdc'] * 100).quantize(Decimal('0.01'))
        
        if total_fees_collected > 0:
            metrics['allocation_efficiency'] = (totals['total_usdc'] / total_fees_collected * 100).quantize(Decimal('0.01'))
            metrics['discrepancy_usd'] = (total_fees_collected - totals['total_usdc']) / Decimal(1e6)
        
        return metrics

    def generate_markdown_summary(self, payload: Dict, groups: Dict[str, List[Dict]], total_fees_collected: Decimal = Decimal(0), recon_data: Dict = None) -> str:
        """Generate markdown version of the summary"""
        total_txs = len(payload["transactions"])
        totals = self.calculate_totals(groups)
        metrics = self.calculate_allocation_metrics(totals, total_fees_collected, recon_data)
        
        md = ["## 📊 Payload Summary\n"]
        md.append(f"**Total Transactions:** {total_txs}\n")
        
        md.append("### USDC Allocations")
        if 'bribes_pct' in metrics:
            if 'core_fees' in metrics and metrics['core_fees'] > 0:
                # Show as percentage of core pool fees
                md.append(f"- **Vote Incentives:** {self.format_amount(str(totals['bribes_usdc']))} ({metrics['vote_incentives_pct_of_core']}% of core pool fees)")
            else:
                md.append(f"- **Vote Incentives:** {self.format_amount(str(totals['bribes_usdc']))} ({metrics['bribes_pct']}% of distributed)")
            
            if 'aura_bribe_pct' in metrics:
                md.append(f"  - Aura: {self.format_amount(str(totals['aura_bribes_usdc']))} ({metrics['aura_bribe_pct']}% of bribes)")
                md.append(f"  - Balancer: {self.format_amount(str(totals['bal_bribes_usdc']))} ({metrics['bal_bribe_pct']}% of bribes)")
            
            md.append(f"- **DAO Fees:** {self.format_amount(str(totals['dao_usdc']))} ({metrics['dao_pct']}% of distributed)")
            md.append(f"- **veBAL Fees:** {self.format_amount(str(totals['vebal_usdc']))} ({metrics['vebal_pct']}% of distributed)")
            md.append(f"- **Partner Fees:** {self.format_amount(str(totals['partner_usdc']))} ({metrics['partner_pct']}% of distributed)")
            md.append(f"- **Beets Fees:** {self.format_amount(str(totals['beets_usdc']))} ({metrics['beets_pct']}% of distributed)")
        else:
            md.append(f"- **Vote Incentives:** {self.format_amount(str(totals['bribes_usdc']))}")
            md.append(f"- **DAO Fees:** {self.format_amount(str(totals['dao_usdc']))}")
            md.append(f"- **veBAL Fees:** {self.format_amount(str(totals['vebal_usdc']))}")
            md.append(f"- **Partner Fees:** {self.format_amount(str(totals['partner_usdc']))}")
            md.append(f"- **Beets Fees:** {self.format_amount(str(totals['beets_usdc']))}")
        md.append("")
        
        if 'core_fees' in metrics and metrics.get('core_fees', 0) > 0:
            md.append("### 🔍 Allocation Validation")
            md.append("")
            md.append("**Fee Pool Breakdown:**")
            md.append(f"- Core pool fees: {self.format_amount(str(metrics['core_fees']))} ({metrics['core_pool_pct']}%)")
            md.append(f"- Non-core pool fees: {self.format_amount(str(metrics['noncore_fees']))} ({Decimal('100') - metrics['core_pool_pct']}%)")
            md.append("")
        
        md.append("### veBAL Transfers")
        md.append(f"- **USDC:** {self.format_amount(str(totals['vebal_usdc']))}")
        md.append(f"- **BAL:** {self.format_amount(str(totals['vebal_bal']), 'BAL')}")
        md.append("")
        
        md.append(f"### 💰 **TOTAL USDC DISTRIBUTED: {self.format_amount(str(totals['total_usdc']))}**")
        
        if 'allocation_efficiency' in metrics:
            md.append(f"\n**Allocation Efficiency:** {metrics['allocation_efficiency']}% of collected fees")
            
            if abs(metrics['discrepancy_usd']) > Decimal("0.01"):
                md.append(f"**⚠️ Discrepancy:** ${metrics['discrepancy_usd']:,.2f}")
        
        return "\n".join(md)
    
    def generate_markdown_table(self, group_name: str, transactions: List[Dict]) -> str:
        """Generate markdown table for a group of transactions"""
        md = [f"\n### {group_name} ({len(transactions)} transactions)\n"]
        
        headers = self.get_table_headers(group_name)
        md.append(f"| {' | '.join(headers)} |")
        md.append(f"|{' | '.join(['-------'] * len(headers))} |")
        
        for tx in transactions:
            data = self.extract_transaction_data(group_name, tx)
            md.append(f"| {data.get('col1', '')} | {data.get('col2', '')} | {data.get('col3', '')} |")
        
        return "\n".join(md)
    
    def export_markdown(self, payload_path: Path, fee_files: List[Path] = None) -> str:
        """Export payload visualization as markdown"""
        # Load payload
        payload = self.parse_payload(payload_path)
        
        # Load fee files if provided
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
        
        # Group transactions
        groups = self.group_transactions(payload["transactions"])
        
        # Build markdown
        md = ["# Fee Allocator Payload Report\n"]
        md.append(f"**File:** {payload_path.name}")
        md.append(f"**Created:** {payload['meta'].get('name', 'Unknown')}")
        
        if fee_details:
            md.append("\n## Fees Collected")
            for detail in fee_details:
                md.append(f"- {detail}")
            md.append(f"- **Total:** ${total_fees_collected/Decimal(1e6):,.2f}")
        
        md.append("")

        recon_data = self.load_recon_data(payload_path)
        
        # Add summary
        md.append(self.generate_markdown_summary(payload, groups, total_fees_collected, recon_data))
        
        # Add transaction tables in priority order
        priority_order = [
            "Aura Bribes",
            "Balancer Bribes", 
            "veBAL Transfers",
            "DAO Transfers",
            "Beets Transfers",
            "Partner Transfers",
            "Token Approvals",
            "Other Bribes",
            "Other Transactions"
        ]
        
        md.append("\n## Transaction Details")
        
        for group_name in priority_order:
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
            "",
            "[bold yellow]USDC Allocations:[/bold yellow]"
        ]
        
        if 'bribes_pct' in metrics:
            # Show vote incentives with percentage of core pool fees if available
            if 'core_fees' in metrics and metrics['core_fees'] > 0:
                lines.append(f"• Vote Incentives: [green]{self.format_amount(str(totals['bribes_usdc']))}[/green] [dim]({metrics['vote_incentives_pct_of_core']}% of core pool fees)[/dim]")
            else:
                lines.append(f"• Vote Incentives: [green]{self.format_amount(str(totals['bribes_usdc']))}[/green] [dim]({metrics['bribes_pct']}% of total)[/dim]")
            
            if 'aura_bribe_pct' in metrics:
                lines.append(f"  [dim]→ Aura: {self.format_amount(str(totals['aura_bribes_usdc']))} ({metrics['aura_bribe_pct']}%)[/dim]")
                lines.append(f"  [dim]→ Balancer: {self.format_amount(str(totals['bal_bribes_usdc']))} ({metrics['bal_bribe_pct']}%)[/dim]")
            
            lines.append(f"• DAO Fees: [blue]{self.format_amount(str(totals['dao_usdc']))}[/blue] [dim]({metrics['dao_pct']}% of total)[/dim]")
            lines.append(f"• veBAL Fees: [magenta]{self.format_amount(str(totals['vebal_usdc']))}[/magenta] [dim]({metrics['vebal_pct']}% of total)[/dim]")
            lines.append(f"• Beets Fees: [cyan]{self.format_amount(str(totals['beets_usdc']))}[/cyan] [dim]({metrics['beets_pct']}% of total)[/dim]")
            lines.append(f"• Partner Fees: [yellow]{self.format_amount(str(totals['partner_usdc']))}[/yellow] [dim]({metrics['partner_pct']}% of total)[/dim]")
        else:
            lines.append(f"• Vote Incentives: [green]{self.format_amount(str(totals['bribes_usdc']))}[/green]")
            lines.append(f"• DAO Fees: [blue]{self.format_amount(str(totals['dao_usdc']))}[/blue]")
            lines.append(f"• veBAL Fees: [magenta]{self.format_amount(str(totals['vebal_usdc']))}[/magenta]")
            lines.append(f"• Beets Fees: [cyan]{self.format_amount(str(totals['beets_usdc']))}[/cyan]")
            lines.append(f"• Partner Fees: [yellow]{self.format_amount(str(totals['partner_usdc']))}[/yellow]")
        
        # Add Allocation Validation section (matching markdown version)
        if 'core_fees' in metrics and metrics.get('core_fees', 0) > 0:
            lines.extend([
                "",
                "[bold yellow]🔍 Allocation Validation[/bold yellow]",
                "",
                "[dim]Fee Pool Breakdown:[/dim]",
                f"• Core pool fees: [cyan]{self.format_amount(str(metrics['core_fees']))}[/cyan] [dim]({metrics['core_pool_pct']}%)[/dim]",
                f"• Non-core pool fees: [magenta]{self.format_amount(str(metrics['noncore_fees']))}[/magenta] [dim]({Decimal('100') - metrics['core_pool_pct']}%)[/dim]"
            ])
        
        lines.extend([
            "",
            "[bold yellow]veBAL Transfers:[/bold yellow]",
            f"• USDC: [green]{self.format_amount(str(totals['vebal_usdc']))}[/green]",
            f"• BAL: [cyan]{self.format_amount(str(totals['vebal_bal']), 'BAL')}[/cyan]",
            "",
            f"[bold red on white] TOTAL USDC DISTRIBUTED: {self.format_amount(str(totals['total_usdc']))} [/bold red on white]"
        ])
        
        if 'allocation_efficiency' in metrics:
            lines.append(f"\n[bold yellow]Allocation Efficiency:[/bold yellow] {metrics['allocation_efficiency']}% of collected fees")
            
            if abs(metrics['discrepancy_usd']) > Decimal("0.01"):
                lines.append(f"[bold red]Discrepancy:[/bold red] ${metrics['discrepancy_usd']:,.2f}")
        
        return Panel("\n".join(lines), title="📊 Payload Summary", border_style="bright_blue")
    
    def create_transaction_table(self, group_name: str, transactions: List[Dict]) -> Table:
        """Create a table for a group of transactions"""
        # Color scheme based on transaction type
        color_map = {
            "Aura Bribes": "cyan",
            "Balancer Bribes": "blue",
            "veBAL Transfers": "magenta",
            "DAO Transfers": "green",
            "Beets Transfers": "cyan",
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
        
        # Add columns based on group
        headers = self.get_table_headers(group_name)
        styles = ["dim", "bold", "dim"] if "Approval" not in group_name else ["dim", "dim", "dim"]
        justifies = ["left", "right", "left"]
        
        for header, style_col, justify in zip(headers, styles, justifies):
            table.add_column(header, style=style_col, justify=justify)
        
        # Add rows
        for tx in transactions:
            data = self.extract_transaction_data(group_name, tx)
            table.add_row(data.get('col1', ''), data.get('col2', ''), data.get('col3', ''))
        
        return table
    
    def visualize_payload(self, payload_path: Path, fee_files: List[Path] = None):
        """Main visualization function"""
        self.console.clear()
        
        # Load payload
        payload = self.parse_payload(payload_path)
        
        # Load fee files if provided
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
        
        # Group transactions
        groups = self.group_transactions(payload["transactions"])
        
        # Create header
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
        
        # Load recon data
        recon_data = self.load_recon_data(payload_path)
        
        # Create and print summary
        summary = self.create_summary_panel(payload, groups, total_fees_collected, recon_data)
        self.console.print(summary)
        self.console.print()
        
        priority_order = [
            "Aura Bribes",
            "Balancer Bribes", 
            "veBAL Transfers",
            "DAO Transfers",
            "Beets Transfers",
            "Partner Transfers",
            "Token Approvals",
            "Other Bribes",
            "Other Transactions"
        ]
        
        for group_name in priority_order:
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
    
    visualize_payload(payload_path, fee_files)


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
    """Generate and save payload report to reports directory
    
    Args:
        payload_path: Path to the payload JSON file
        fee_files: Optional list of fee collection JSON files
        output_path: Optional output path. If not provided, saves to reports directory
        
    Returns:
        Path to the saved report
    """
    from fee_allocator.accounting import PROJECT_ROOT
    
    # Generate markdown content
    markdown_content = export_markdown(payload_path, fee_files)
    
    if output_path is None:
        # Extract date from payload filename
        date_str = payload_path.stem
        if date_str.startswith(("v2_", "v3_")):
            date_str = date_str[3:]
        
        # Save to reports directory
        reports_dir = Path(PROJECT_ROOT) / "fee_allocator" / "reports"
        reports_dir.mkdir(exist_ok=True, parents=True)
        output_path = reports_dir / f"{date_str}.md"
    
    output_path.write_text(markdown_content)
    print(f"\nMarkdown report saved to: {output_path}")
    return output_path


def save_combined_report(payload_path: Path, v2_fees_file: Path = None, v3_fees_file: Path = None) -> Path:
    """Generate and save combined payload report to reports directory
    
    This is a convenience wrapper that handles the v2/v3 fee files specifically.
    """
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
    parser.add_argument("--output", type=Path, help="Output file for markdown export (defaults to reports directory)")
    parser.add_argument("--v2-fees", type=Path, help="Path to v2 fees JSON file")
    parser.add_argument("--v3-fees", type=Path, help="Path to v3 fees JSON file")
    
    args = parser.parse_args()
    
    if args.markdown:
        markdown_content = export_combined_markdown(args.payload_file, args.v2_fees, args.v3_fees)
        if args.output:
            output_path = args.output
        else:
            # Default to reports directory with same filename as payload
            reports_dir = Path(__file__).parent / "reports"
            reports_dir.mkdir(exist_ok=True)
            output_path = reports_dir / args.payload_file.with_suffix('.md').name
            
        if args.output or not output_path.exists():
            output_path.parent.mkdir(exist_ok=True, parents=True)
            output_path.write_text(markdown_content)
            print(f"Markdown exported to {output_path}")
        else:
            print(markdown_content)
    else:
        visualize_combined_payload(args.payload_file, args.v2_fees, args.v3_fees)