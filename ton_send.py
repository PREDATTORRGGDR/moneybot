"""Автоотправка GRAM (TON) с горячего кошелька бота."""
import asyncio
import os
from decimal import Decimal

from ton_core import NetworkGlobalID
from tonutils.clients import ToncenterClient
from tonutils.contracts import TONTransferBuilder, WalletTg, WalletV4R2, WalletV5R1

MNEMONIC = os.getenv("SEND_MNEMONIC", "").strip()
WALLET_CLS = {"v5r1": WalletV5R1, "v4r2": WalletV4R2, "tg": WalletTg}[os.getenv("SEND_WALLET_VERSION", "v5r1").lower()]
TONCENTER_KEY = os.getenv("TONCENTER_API_KEY", "") or None
FEE_RESERVE = 50_000_000  # 0.05 GRAM на комиссию

ENABLED = bool(MNEMONIC)
_lock = asyncio.Lock()  # по одной отправке за раз, иначе seqno перепутается


class NotSent(Exception):
    """Перевод точно не ушёл — можно отправить вручную."""


class Unconfirmed(Exception):
    """Перевод мог уйти — сначала проверь кошелёк, не отправляй повторно."""


def destination(recipient):
    # @username резолвится через TON DNS (username.t.me) — работает только для
    # юзернеймов, к которым привязан кошелёк (коллекционные с Fragment)
    return recipient[1:] + ".t.me" if recipient.startswith("@") else recipient


async def send_gram(recipient, amount, comment=None):
    """Отправляет amount GRAM и ждёт подтверждения. Возвращает адрес кошелька бота (для ссылки)."""
    nano = int(Decimal(str(amount)) * 10**9)
    async with _lock:
        client = ToncenterClient(NetworkGlobalID.MAINNET, api_key=TONCENTER_KEY)
        await client.connect()
        try:
            try:
                wallet, *_ = WALLET_CLS.from_mnemonic(client, MNEMONIC)
                await wallet.refresh()
                if wallet.balance < nano + FEE_RESERVE:
                    raise NotSent(f"на кошельке бота мало GRAM: {wallet.balance / 1e9:g}")
                before = await wallet.seqno() if wallet.is_active else 0
                # bounce=False: кошелёк получателя ещё не активирован, иначе перевод вернётся
                ext = await wallet.build_external_message([TONTransferBuilder(
                    destination=destination(recipient), amount=nano, body=comment, bounce=False)])
            except NotSent:
                raise
            except Exception as e:
                raise NotSent(str(e) or type(e).__name__) from e

            try:
                await client.send_message(ext.as_hex)
            except Exception as e:
                raise Unconfirmed(f"ошибка при отправке: {e}") from e

            for _ in range(30):  # до ~90 сек ждём, пока seqno вырастет
                await asyncio.sleep(3)
                try:
                    await wallet.refresh()
                    if wallet.is_active and await wallet.seqno() > before:
                        return wallet.address.to_str(is_bounceable=False)
                except Exception:
                    pass
            raise Unconfirmed("перевод не подтвердился за 90 сек")
        finally:
            await client.close()
