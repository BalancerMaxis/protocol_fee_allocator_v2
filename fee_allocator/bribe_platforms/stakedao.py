from typing import Dict, Optional, Tuple, Any
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
    SUPPORTED_L2_CHAINS = ["arbitrum", "optimism", "base", "polygon"]
    AURA_VEBAL_LOCKER = Web3.to_checksum_address("0xaF52695E1bB01A16D33D7194C28C42b10e0Dbec2")
    BASE_GAS_LIMIT = 50000
    GAS_BUFFER_MULTIPLIER = 1.25
    ABI_DIR = Path(__file__).parent.parent / "abi"
    CCIP_ROUTERS = {
        "mainnet": Web3.to_checksum_address("0x80226fc0Ee2b096224EeAc085Bb9a8cba1146f7D"),
        "arbitrum": Web3.to_checksum_address("0x141fa059441E0ca23ce184B6A78bafD2A517DdE8"),
        "optimism": Web3.to_checksum_address("0x3206695CaE29952f4b0c22a169725a865bc8Ce0f"),
        "base": Web3.to_checksum_address("0x881e3A65B4d4a04dD529061dd0071cf975F58bCD"),
        "polygon": Web3.to_checksum_address("0x849c5ED5a80F5B408Dd4969b78c2C8fdf0565Bfe"),
    }

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
        for chain in self.run_config.all_chains:
            for pool in chain.core_pools:
                if pool.gauge_address:
                    self._gauge_to_chain_cache[pool.gauge_address.lower()] = chain.name

    def _load_abi(self, name: str):
        with open(self.ABI_DIR / f"{name}.json", 'r') as f:
            return json.load(f)

    def _get_chain_selector(self, chain_id: int) -> int:
        adapter_abi = self._load_abi("laposte_adapter")

        adapter_contract = self.w3.eth.contract(
            address=Web3.to_checksum_address(self.laposte_adapter_address),
            abi=adapter_abi
        )

        try:
            return adapter_contract.functions.getBridgeChainId(chain_id).call()
        except Exception as e:
            logger.error(f"Failed to get chain selector for chain {chain_id}: {e}")
            raise

    def _build_laposte_message(self, destination_chain_id: int, campaign_params: tuple, votemarket_address: str, sender_address: str) -> bytes:
        payload_params = encode(
            ['(uint256,address,address,address,uint8,uint256,uint256,address[],address,bool)'],
            [campaign_params]
        )
        payload = encode(
            ['(uint8,address,address,bytes)'],
            [(0, sender_address, votemarket_address, payload_params)]
        )

        token = self.w3.eth.contract(address=Web3.to_checksum_address(self.usdc_address), abi=self._load_abi("ERC20"))
        token_name = token.functions.name().call()
        token_symbol = token.functions.symbol().call()
        token_decimals = token.functions.decimals().call()

        laposte = self.w3.eth.contract(address=Web3.to_checksum_address(self.laposte_address), abi=self._load_abi("laposte"))
        nonce = laposte.functions.sentNonces(destination_chain_id).call() + 1

        return encode(
            ['(uint256,address,address,(address,uint256)[],(string,string,uint8)[],bytes,uint256)'],
            [(
                destination_chain_id,
                self.campaign_remote_manager_address,
                self.campaign_remote_manager_address,
                [(self.usdc_address, campaign_params[6])],
                [(token_name, token_symbol, token_decimals)],
                payload,
                nonce
            )]
        )

    def _simulate_ccip_receive(self, destination_chain_name: str, laposte_message: bytes) -> int:
        dest_w3 = Web3Rpc(destination_chain_name, os.environ.get("DRPC_KEY"))
        source_chain_selector = self._get_chain_selector(1)

        message_id = Web3.keccak(text='gas_estimation')
        any2evm_message = (
            message_id,
            source_chain_selector,
            encode(['address'], [self.laposte_address]),
            laposte_message,
            []
        )

        ccip_receive_sig = Web3.keccak(text='ccipReceive((bytes32,uint64,bytes,bytes,(address,uint256)[]))').hex()[:8]
        encoded_params = encode(
            ['(bytes32,uint64,bytes,bytes,(address,uint256)[])'],
            [any2evm_message]
        )
        calldata = bytes.fromhex(ccip_receive_sig) + encoded_params

        estimated_gas = dest_w3.eth.estimate_gas({
            'from': self.CCIP_ROUTERS[destination_chain_name],
            'to': self.laposte_adapter_address,
            'data': '0x' + calldata.hex()
        })

        base_gas = estimated_gas - self.BASE_GAS_LIMIT
        additional_gas_limit = int(base_gas * self.GAS_BUFFER_MULTIPLIER)

        logger.info(f"CCIP gas estimation: base={base_gas}, with_buffer={additional_gas_limit}, total={additional_gas_limit + self.BASE_GAS_LIMIT}")
        return additional_gas_limit

    def _calculate_ccip_fee(self, destination_chain_id: int, destination_chain_name: str, campaign_params: tuple, votemarket_address: str, sender_address: str) -> Tuple[int, int]:
        destination_selector = self._get_chain_selector(destination_chain_id)

        router_contract = self.w3.eth.contract(
            address=self.ccip_router_address,
            abi=self._load_abi("ccip_router")
        )

        laposte_message = self._build_laposte_message(destination_chain_id, campaign_params, votemarket_address, sender_address)
        additional_gas_limit = self._simulate_ccip_receive(destination_chain_name, laposte_message)
        total_gas_limit = additional_gas_limit + self.BASE_GAS_LIMIT

        evm_extra_args_tag = bytes.fromhex('97a657c9')
        extra_args_data = encode(['uint256'], [total_gas_limit])
        evm_extra_args = evm_extra_args_tag + extra_args_data

        ccip_message = {
            'receiver': encode(['address'], [self.laposte_adapter_address]),
            'data': laposte_message,
            'tokenAmounts': [],
            'feeToken': '0x0000000000000000000000000000000000000000',
            'extraArgs': evm_extra_args
        }

        fee = router_contract.functions.getFee(
            destination_selector,
            ccip_message
        ).call()

        fee_with_buffer = int(fee * 1.5)

        logger.info(f"CCIP fee for chain {destination_chain_id}: {Web3.from_wei(fee_with_buffer, 'ether')} ETH (gas_limit={total_gas_limit})")
        return fee_with_buffer, additional_gas_limit

    def process_bribes(self, bribes_df: pd.DataFrame, builder: Any, usdc: Any) -> None:
        if bribes_df.empty or bribes_df["amount"].sum() == 0:
            logger.info("No bribes to process for StakeDAO")
            return

        campaign_manager = SafeContract(
            self.campaign_remote_manager_address,
            abi_file_path=str(self.ABI_DIR / "stakedao_marketv2.json")
        )

        total_usdc = sum(int(row["amount"] * 1e6) for _, row in bribes_df.iterrows() if row["amount"] > 0)
        if total_usdc > 0:
            usdc.approve(self.campaign_remote_manager_address, total_usdc)
            logger.info(f"Approved {total_usdc / 1e6} USDC for StakeDAO CampaignRemoteManager")

        for _, row in bribes_df.iterrows():
            if int(row["amount"]) == 0:
                continue

            gauge_address = Web3.to_checksum_address(row["target"])
            chain_name = self._gauge_to_chain_cache.get(gauge_address.lower())
            chain_id = AddrBook.chain_ids_by_name.get(chain_name)

            if not chain_name or not chain_id:
                raise ValueError(f"Cannot resolve chain for gauge {gauge_address}")

            mantissa = round(row["amount"] * 1e6)

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

            is_alliance = row["is_alliance"]
            voting_override = row.get("voting_pool_override")

            aura_only = is_alliance or voting_override == "aura"
            bal_only = voting_override == "bal"
            addresses = [self.AURA_VEBAL_LOCKER] if aura_only or bal_only else []
            is_whitelist = aura_only

            campaign_params = (
                chain_id,
                gauge_address,
                builder.safe_address,
                self.usdc_address,
                2,
                mantissa,
                mantissa,
                addresses,
                "0x0000000000000000000000000000000000000000",
                is_whitelist,
            )

            ccip_fee, additional_gas_limit = self._calculate_ccip_fee(destination_chain_id, destination_chain_name, campaign_params, vote_market_v2_address, builder.safe_address)

            campaign_manager.createCampaign(
                campaign_params,
                destination_chain_id,
                additional_gas_limit,
                vote_market_v2_address,
                value=ccip_fee
            )
            eth_amount = Web3.from_wei(ccip_fee, 'ether')

            mode_tag = " [AURA only]" if aura_only else " [BAL only]" if bal_only else ""
            logger.info(f"Created StakeDAO v2 bribe for {chain_name} gauge {gauge_address} (campaign on {destination_chain_name}): ${row['amount']:.2f} USDC{mode_tag} (includes {eth_amount:.6f} ETH for CCIP)")

    def validate_gauge_requirements(self, gauge_address: str) -> Tuple[bool, Optional[str]]:
        """StakeDAO doesn't have specific gauge requirements"""
        return True, None

    @property
    def platform_name(self) -> str:
        return "stakedao"
