import logging
import ssl
from enum import IntEnum
from typing import Optional, Dict, Any

import certifi
from aiohttp import ClientSession, ClientTimeout, TCPConnector
from eth_account.signers.local import LocalAccount
from eth_typing import ChecksumAddress
from web3 import AsyncHTTPProvider, AsyncWeb3, WebsocketProviderV2
from web3.middleware.signing import \
    async_construct_sign_and_send_raw_middleware
from web3.providers.async_base import AsyncBaseProvider
from web3.types import TxParams

from crynux_sdk.config import TxOption

from . import crynux_token, network_stats, node, qos, task, task_queue
from .exceptions import TxRevertedError
from .utils import TxWaiter

__all__ = ["TxRevertedError", "Contracts", "TxWaiter", "get_contracts", "set_contracts"]

_logger = logging.getLogger(__name__)


class ProviderType(IntEnum):
    HTTP = 0
    WS = 1
    Other = 2

class Contracts(object):
    node_contract: node.NodeContract
    task_contract: task.TaskContract
    token_contract: crynux_token.TokenContract
    qos_contract: qos.QOSContract
    task_queue_contract: task_queue.TaskQueueContract
    netstats_contract: network_stats.NetworkStatsContract

    def __init__(
        self,
        provider: Optional[AsyncBaseProvider] = None,
        provider_path: Optional[str] = None,
        privkey: str = "",
        default_account_index: Optional[int] = None,
        timeout: float = 30,
    ):
        self._w3 = None
        self._session = None
        self.provider_type: ProviderType = ProviderType.Other
        if provider is None:
            if provider_path is None:
                raise ValueError("provider and provider_path cannot be all None.")
            if provider_path.startswith("http"):
                self.provider_type = ProviderType.HTTP
                self.provider = AsyncHTTPProvider(provider_path)
                ssl_context = ssl.create_default_context(cafile=certifi.where())
                session = ClientSession(timeout=ClientTimeout(timeout), connector=TCPConnector(ssl=ssl_context))
                self._session = session
            elif provider_path.startswith("ws"):
                self.provider_type = ProviderType.WS
                self.provider = WebsocketProviderV2(provider_path, call_timeout=timeout)
            else:
                raise ValueError(f"unsupported provider {provider_path}")
        else:
            self.provider = provider
            self._w3 = AsyncWeb3(provider)

        self._privkey = privkey
        self._default_account_index = default_account_index

        self._initialized = False
        self._closed = False

    async def init(
        self,
        token_contract_address: Optional[str] = None,
        node_contract_address: Optional[str] = None,
        task_contract_address: Optional[str] = None,
        qos_contract_address: Optional[str] = None,
        task_queue_contract_address: Optional[str] = None,
        netstats_contract_address: Optional[str] = None,
        *,
        option: "Optional[TxOption]" = None,
    ):
        if self._w3 is None:
            if self.provider_type == ProviderType.HTTP:
                assert isinstance(self.provider, AsyncHTTPProvider)
                assert self._session is not None
                await self.provider.cache_async_session(session=self._session)
                self._w3 = AsyncWeb3(self.provider)
            elif self.provider_type == ProviderType.WS:
                assert isinstance(self.provider, WebsocketProviderV2)
                self._w3 = AsyncWeb3.persistent_websocket(self.provider)
                await self._w3.provider.connect()
            else:
                raise ValueError("Unknown provider type")

        if self._privkey != "":
            account: LocalAccount = self._w3.eth.account.from_key(self._privkey)
            middleware = await async_construct_sign_and_send_raw_middleware(account)
            self._w3.middleware_onion.add(middleware)
            self._w3.eth.default_account = account.address
        elif self._default_account_index is not None:
            self._w3.eth.default_account = (await self._w3.eth.accounts)[
                self._default_account_index
            ]
        _logger.info(f"Wallet address is {self._w3.eth.default_account}")

        if token_contract_address is not None:
            self.token_contract = crynux_token.TokenContract(
                self.w3, self.w3.to_checksum_address(token_contract_address)
            )
        else:
            self.token_contract = crynux_token.TokenContract(self.w3)
            await self.token_contract.deploy(option=option)
            token_contract_address = self.token_contract.address

        if qos_contract_address is not None:
            self.qos_contract = qos.QOSContract(
                self.w3, self.w3.to_checksum_address(qos_contract_address)
            )
        elif task_contract_address is None:
            # task contract has not been deployed, need deploy qos contract
            self.qos_contract = qos.QOSContract(self.w3)
            await self.qos_contract.deploy(option=option)
            qos_contract_address = self.qos_contract.address

        if task_queue_contract_address is not None:
            self.task_queue_contract = task_queue.TaskQueueContract(
                self.w3, self.w3.to_checksum_address(task_queue_contract_address)
            )
        elif task_contract_address is None:
            # task contract has not been deployed, need deploy qos contract
            self.task_queue_contract = task_queue.TaskQueueContract(self.w3)
            await self.task_queue_contract.deploy(option=option)
            task_queue_contract_address = self.task_queue_contract.address

        if netstats_contract_address is not None:
            self.netstats_contract = network_stats.NetworkStatsContract(
                self.w3, self.w3.to_checksum_address(netstats_contract_address)
            )
        elif task_contract_address is None:
            # task contract has not been deployed, need deploy qos contract
            self.netstats_contract = network_stats.NetworkStatsContract(self.w3)
            await self.netstats_contract.deploy(option=option)
            netstats_contract_address = self.netstats_contract.address

        if node_contract_address is not None:
            self.node_contract = node.NodeContract(
                self.w3, self.w3.to_checksum_address(node_contract_address)
            )
        else:
            assert qos_contract_address is not None, "QOS contract address is None"
            assert netstats_contract_address is not None, "NetworkStats contract address is None"
            self.node_contract = node.NodeContract(self.w3)
            await self.node_contract.deploy(
                token_contract_address,
                qos_contract_address,
                netstats_contract_address,
                option=option,
            )
            node_contract_address = self.node_contract.address
            await self.qos_contract.update_node_contract_address(
                node_contract_address, option=option
            )
            await self.netstats_contract.update_node_contract_address(
                node_contract_address, option=option
            )

        if task_contract_address is not None:
            self.task_contract = task.TaskContract(
                self.w3, self.w3.to_checksum_address(task_contract_address)
            )
        else:
            assert qos_contract_address is not None, "QOS contract address is None"
            assert task_queue_contract_address is not None, "Task queue contract address is None"
            assert netstats_contract_address is not None, "NetworkStats contract address is None"

            self.task_contract = task.TaskContract(self.w3)
            await self.task_contract.deploy(
                node_contract_address,
                token_contract_address,
                qos_contract_address,
                task_queue_contract_address,
                netstats_contract_address,
                option=option,
            )
            task_contract_address = self.task_contract.address

            await self.node_contract.update_task_contract_address(
                task_contract_address, option=option
            )
            await self.qos_contract.update_task_contract_address(
                task_contract_address, option=option
            )
            await self.task_queue_contract.update_task_contract_address(
                task_contract_address, option=option
            )
            await self.netstats_contract.update_task_contract_address(
                task_contract_address, option=option
            )

        self._initialized = True

        return self

    async def close(self):
        if not self._closed:
            if self.provider_type == ProviderType.HTTP:
                if self._session is not None and not self._session.closed:
                    await self._session.close()
            elif self.provider_type == ProviderType.WS:
                assert isinstance(self.provider, WebsocketProviderV2)
                await self.provider.disconnect()
            self._closed = True

    async def __aenter__(self):
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return await self.close()

    def get_contract(self, name: str):
        if name == "token":
            return self.token_contract
        elif name == "node":
            return self.node_contract
        elif name == "task":
            return self.task_contract
        elif name == "qos":
            return self.qos_contract
        elif name == "task_queue":
            return self.task_queue_contract
        elif name == "netstats":
            return self.netstats_contract
        else:
            raise ValueError(f"unknown contract name {name}")
    
    async def get_events(
        self,
        contract_name: str,
        event_name: str,
        filter_args: Optional[Dict[str, Any]] = None,
        from_block: Optional[int] = None,
        to_block: Optional[int] = None,
    ):
        contract = self.get_contract(contract_name)
        return await contract.get_events(
            event_name=event_name,
            filter_args=filter_args,
            from_block=from_block,
            to_block=to_block
        )

    @property
    def w3(self):
        assert self._w3 is not None
        return self._w3

    @property
    def account(self) -> ChecksumAddress:
        res = self.w3.eth.default_account
        assert res, "Contracts has not been initialized!"
        return res

    @property
    def initialized(self) -> bool:
        return self._initialized

    async def get_current_block_number(self) -> int:
        return await self.w3.eth.get_block_number()

    async def get_balance(self, account: ChecksumAddress) -> int:
        return await self.w3.eth.get_balance(account)

    async def transfer(
        self, to: str, amount: int, *, option: "Optional[TxOption]" = None
    ):
        opt: TxParams = {}
        if option is not None:
            opt.update(**option)
        opt["to"] = self.w3.to_checksum_address(to)
        opt["from"] = self.account
        opt["value"] = self.w3.to_wei(amount, "Wei")

        tx_hash = await self.w3.eth.send_transaction(opt)
        receipt = await self.w3.eth.wait_for_transaction_receipt(tx_hash)
        return receipt


_default_contracts: Optional[Contracts] = None


def get_contracts() -> Contracts:
    assert _default_contracts is not None, "Contracts has not been set."

    return _default_contracts


def set_contracts(contracts: Contracts):
    global _default_contracts

    _default_contracts = contracts
