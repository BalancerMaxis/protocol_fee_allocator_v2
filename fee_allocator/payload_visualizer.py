import json
from pathlib import Path
from decimal import Decimal
from typing import Dict, List, Any
from collections import defaultdict

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.layout import Layout
from rich.columns import Columns
from rich.text import Text
from rich import box
from rich.tree import Tree

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
                else:
                    # Check if it's a partner transfer
                    groups["Partner Transfers"].append(tx)
            else:
                groups["Other Transactions"].append(tx)
        
        return dict(groups)
    
    def create_summary_panel(self, payload: Dict, groups: Dict[str, List[Dict]], total_fees_collected: Decimal = Decimal(0)) -> Panel:
        """Create summary statistics panel"""
        total_txs = len(payload["transactions"])
        
        # Calculate totals
        total_bribes_usdc = Decimal(0)
        total_dao_usdc = Decimal(0)
        total_vebal_usdc = Decimal(0)
        total_vebal_bal = Decimal(0)
        total_partner_usdc = Decimal(0)
        
        for group_name, txs in groups.items():
            for tx in txs:
                if "Bribe" in group_name:
                    if tx.get("contractInputsValues", {}).get("_token", "").lower() == self.book.get("tokens/USDC", "").lower():
                        total_bribes_usdc += Decimal(tx["contractInputsValues"]["_amount"])
                elif group_name == "veBAL Transfers":
                    if tx.get("to", "").lower() == self.book.get("tokens/USDC", "").lower():
                        total_vebal_usdc += Decimal(tx["contractInputsValues"]["_value"])
                    elif tx.get("to", "").lower() == self.book.get("tokens/BAL", "").lower():
                        total_vebal_bal += Decimal(tx["contractInputsValues"]["_value"])
                elif group_name == "DAO Transfers":
                    total_dao_usdc += Decimal(tx["contractInputsValues"]["_value"])
                elif group_name == "Partner Transfers":
                    if tx.get("to", "").lower() == self.book.get("tokens/USDC", "").lower():
                        total_partner_usdc += Decimal(tx["contractInputsValues"]["_value"])
        
        total_usdc_distributed = total_bribes_usdc + total_dao_usdc + total_vebal_usdc + total_partner_usdc
        
        summary_text = f"""[bold cyan]Transaction Summary[/bold cyan]
        
Total Transactions: [bold]{total_txs}[/bold]

[bold yellow]USDC Allocations:[/bold yellow]
• Vote Incentives: [green]{self.format_amount(str(total_bribes_usdc))}[/green]
• DAO Fees: [blue]{self.format_amount(str(total_dao_usdc))}[/blue]
• veBAL Fees: [magenta]{self.format_amount(str(total_vebal_usdc))}[/magenta]
• Partner Fees: [yellow]{self.format_amount(str(total_partner_usdc))}[/yellow]

[bold yellow]veBAL Transfers:[/bold yellow]
• USDC: [green]{self.format_amount(str(total_vebal_usdc))}[/green]
• BAL: [cyan]{self.format_amount(str(total_vebal_bal), "BAL")}[/cyan]

[bold red on white] TOTAL USDC DISTRIBUTED: {self.format_amount(str(total_usdc_distributed))} [/bold red on white]
"""
        
        if total_fees_collected > 0:
            percentage = (total_usdc_distributed / total_fees_collected) * 100
            summary_text += f"\n[bold yellow]Allocation Efficiency:[/bold yellow] {percentage:.2f}% of collected fees"
            
            # Show discrepancy if any (convert to USD for display)
            discrepancy_usd = (total_fees_collected - total_usdc_distributed) / Decimal(1e6)
            if abs(discrepancy_usd) > Decimal("0.01"):
                summary_text += f"\n[bold red]Discrepancy:[/bold red] ${discrepancy_usd:,.2f}"
        
        return Panel(summary_text, title="📊 Payload Summary", border_style="bright_blue")
    
    def create_transaction_table(self, group_name: str, transactions: List[Dict]) -> Table:
        """Create a table for a group of transactions"""
        # Color scheme based on transaction type
        color_map = {
            "Aura Bribes": "cyan",
            "Balancer Bribes": "blue",
            "veBAL Transfers": "magenta",
            "DAO Transfers": "green",
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
        
        # Add columns based on transaction type
        if "Bribe" in group_name:
            table.add_column("Gauge/Proposal", style="dim")
            table.add_column("Amount", justify="right", style="bold")
            table.add_column("Token", style="dim")
            
            for tx in transactions:
                prop_hash = tx.get("contractInputsValues", {}).get("_proposal", "")[:10] + "..."
                amount = tx.get("contractInputsValues", {}).get("_amount", "0")
                token = self.format_address(tx.get("contractInputsValues", {}).get("_token", ""))
                
                table.add_row(
                    prop_hash,
                    self.format_amount(amount),
                    token
                )
        
        elif group_name in ["veBAL Transfers", "DAO Transfers", "Partner Transfers"]:
            table.add_column("Recipient", style="dim")
            table.add_column("Amount", justify="right", style="bold")
            table.add_column("Token", style="dim")
            
            for tx in transactions:
                recipient = self.format_address(tx.get("contractInputsValues", {}).get("_to", ""))
                amount = tx.get("contractInputsValues", {}).get("_value", "0")
                token_addr = tx.get("to", "")
                
                if token_addr.lower() == self.book.get("tokens/USDC", "").lower():
                    amount_fmt = self.format_amount(amount, "USDC")
                    token = "USDC"
                elif token_addr.lower() == self.book.get("tokens/BAL", "").lower():
                    amount_fmt = self.format_amount(amount, "BAL")
                    token = "BAL"
                else:
                    amount_fmt = amount
                    token = self.format_address(token_addr)
                
                table.add_row(recipient, amount_fmt, token)
        
        elif group_name == "Token Approvals":
            table.add_column("Token", style="dim")
            table.add_column("Spender", style="dim")
            table.add_column("Amount", justify="right", style="dim")
            
            for tx in transactions:
                token = self.format_address(tx.get("to", ""))
                spender = self.format_address(tx.get("contractInputsValues", {}).get("_spender", ""))
                amount = tx.get("contractInputsValues", {}).get("_value", "0")
                
                table.add_row(token, spender, self.format_amount(amount))
        
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
        
        # Create and print summary
        summary = self.create_summary_panel(payload, groups, total_fees_collected)
        self.console.print(summary)
        self.console.print()
        
        priority_order = [
            "Aura Bribes",
            "Balancer Bribes", 
            "veBAL Transfers",
            "DAO Transfers",
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