from typing import Dict, Optional, Tuple, Any, List
import pandas as pd
from web3 import Web3
from .base import BribePlatform
from bal_tools.safe_tx_builder import SafeContract
from bal_tools import Web3Rpc
from pathlib import Path
from fee_allocator.logger import logger
from bal_addresses import AddrBook
from eth_abi import encode
import json
import os


class StakeDAOPlatform(BribePlatform):
    """StakeDAO VoteMarket v2 platform implementation for Balancer bribes"""

    SUPPORTED_L2_CHAINS = ["arbitrum", "optimism", "base", "polygon"]

    def __init__(self, book: Dict[str, str], run_config: Any):
        super().__init__(book, run_config)

        self.ccip_router_address = book["chainlink/ccip_router"]
        self.laposte_address = book["stake_dao/laposte"]
        self.laposte_adapter_address = book["stake_dao/laposte_adapter"]
        self.campaign_remote_manager_address = book["stake_dao/campaign_remote_manager_v2"]

        self.usdc_address = book["tokens/USDC"]
        self._gauge_to_chain_cache = {}
        self._build_gauge_to_chain_map()

        self.w3 = Web3Rpc("mainnet", os.environ.get("DRPC_KEY"))

    def _build_gauge_to_chain_map(self):
        """Build a mapping of gauge addresses to their chains from the run config."""
        if not self.run_config or not hasattr(self.run_config, 'all_chains'):
            return

        for chain in self.run_config.all_chains:
            for pool in chain.core_pools:
                if pool.gauge_address:
                    self._gauge_to_chain_cache[pool.gauge_address.lower()] = chain.name

    def _get_chain_selector(self, chain_id: int) -> int:
        """Get CCIP chain selector for a given chain ID."""
        base_dir = Path(__file__).parent.parent
        with open(f"{base_dir}/abi/laposte_adapter.json", 'r') as f:
            adapter_abi = json.load(f)

        adapter_contract = self.w3.eth.contract(
            address=Web3.to_checksum_address(self.laposte_adapter_address),
            abi=adapter_abi
        )

        try:
            selector = adapter_contract.functions.getBridgeChainId(chain_id).call()
            return selector
        except Exception as e:
            logger.error(f"Failed to get chain selector for chain {chain_id}: {e}")
            raise

    def _calculate_ccip_fee(self, destination_chain_id: int, campaign_params: tuple) -> int:
        """Calculate CCIP fee for cross-chain message."""
        destination_selector = self._get_chain_selector(destination_chain_id)

        base_dir = Path(__file__).parent.parent
        with open(f"{base_dir}/abi/ccip_router.json", 'r') as f:
            router_abi = json.load(f)

        router_contract = self.w3.eth.contract(
            address=self.ccip_router_address,
            abi=router_abi
        )

        payload_data = encode(
            ['(uint256,address,address,address,uint8,uint256,uint256,address[],address,bool)'],
            [campaign_params]
        )

        laposte_message = encode(
            ['(uint256,address,address,(address,uint256)[],bytes)'],
            [(
                destination_chain_id,
                self.campaign_remote_manager_address,  # to
                self.campaign_remote_manager_address,  # sender
                [(self.usdc_address, campaign_params[6])],  # token transfer
                payload_data
            )]
        )

        gas_limit = 200000
        evm_extra_args_tag = bytes.fromhex('97a657c9')  # EVMExtraArgsV1 tag
        extra_args_data = encode(['uint256'], [gas_limit])
        evm_extra_args = evm_extra_args_tag + extra_args_data

        ccip_message = {
            'receiver': encode(['address'], [self.laposte_adapter_address]),
            'data': laposte_message,
            'tokenAmounts': [],  # No tokens via CCIP (handled by LaPoste)
            'feeToken': '0x0000000000000000000000000000000000000000',  # Native ETH
            'extraArgs': evm_extra_args
        }

        fee = router_contract.functions.getFee(
            destination_selector,
            ccip_message
        ).call()

        # 50% buffer for safety
        fee_with_buffer = int(fee * 1.50)

        logger.info(f"CCIP fee for chain {destination_chain_id}: {Web3.from_wei(fee_with_buffer, 'ether')} ETH (with 50% buffer)")
        return fee_with_buffer

    def process_bribes(self, bribes_df: pd.DataFrame, builder: Any, usdc: Any) -> None:
        balancer_bribes = bribes_df[bribes_df["platform"] == "balancer"]

        if balancer_bribes.empty or balancer_bribes["amount"].sum() == 0:
            logger.info("No Balancer bribes to process for StakeDAO")
            return

        base_dir = Path(__file__).parent.parent
        campaign_manager = SafeContract(
            self.campaign_remote_manager_address,
            abi_file_path=f"{base_dir}/abi/stakedao_marketv2.json"
        )

        total_usdc = sum(int(row["amount"] * 1e6) for _, row in balancer_bribes.iterrows() if row["amount"] > 0)
        if total_usdc > 0:
            usdc.approve(self.campaign_remote_manager_address, total_usdc)
            logger.info(f"Approved {total_usdc / 1e6} USDC for StakeDAO CampaignRemoteManager")

        for _, row in balancer_bribes.iterrows():
            if int(row["amount"]) == 0:
                continue

            gauge_address = Web3.to_checksum_address(row["target"])
            chain_name = self._gauge_to_chain_cache.get(gauge_address.lower())
            chain_id = AddrBook.chain_ids_by_name.get(chain_name)

            if not chain_name or not chain_id:
                raise ValueError(f"Cannot resolve chain for gauge {gauge_address}")

            mantissa = round(row["amount"] * 1e6)

            # Mainnet gauges route to Arbitrum, L2 gauges stay on same chain
            if chain_name == "mainnet":
                destination_chain_name = "arbitrum"
                destination_chain_id = AddrBook.chain_ids_by_name["arbitrum"]
            elif chain_name in self.SUPPORTED_L2_CHAINS:
                destination_chain_name = chain_name
                destination_chain_id = chain_id
            else:
                raise ValueError(f"Chain {chain_name} not supported by StakeDAO v2. Supported chains: mainnet, {', '.join(self.SUPPORTED_L2_CHAINS)}")

            destination_book = AddrBook(destination_chain_name)
            vote_market_v2_address = destination_book.flatbook["stake_dao/votemarket_v2"]

            campaign_params = (
                chain_id,  # chainId (of the gauge)
                gauge_address,  # gauge
                builder.safe_address,  # manager
                self.usdc_address,  # rewardToken
                2,  # numberOfPeriods
                mantissa,  # maxRewardPerVote
                mantissa,  # totalRewardAmount
                [],  # whitelist
                "0x0000000000000000000000000000000000000000",  # hook
                False  # isWhitelist
            )

            ccip_fee = self._calculate_ccip_fee(destination_chain_id, campaign_params)

            campaign_manager.createCampaign(
                campaign_params,
                destination_chain_id,
                0,  # additionalGasLimit (using default)
                vote_market_v2_address,
                value=ccip_fee
            )
            eth_amount = Web3.from_wei(ccip_fee, 'ether')

            logger.info(f"Created StakeDAO v2 bribe for {chain_name} gauge {gauge_address} (campaign on {destination_chain_name}): ${row['amount']:.2f} USDC (includes {eth_amount:.6f} ETH for CCIP)")

    def get_total_approval_amount(self, bribes_df: pd.DataFrame) -> int:
        """Returns 0 because approvals are handled in process_bribes method."""
        return 0

    def validate_gauge_requirements(self, gauge_address: str) -> Tuple[bool, Optional[str]]:
        """StakeDAO doesn't have specific gauge requirements"""
        return True, None

    @property
    def platform_name(self) -> str:
        return "stakedao"

    @property
    def supported_markets(self) -> List[str]:
        return ["balancer"]

    def get_platform_for_market(self, market: str, voting_pool_override: Optional[str]) -> str:
        if market == "aura":
            return "hh"

        if market == "balancer":
            if voting_pool_override == "aura":
                return "hh"
            return "stakedao"

        return "hh"