"""Worker job: read the Webull paper account and store one `equity_snapshots` row.

Scope (deliberately minimal): read-only broker calls (`list_accounts` +
`get_account_snapshot`) plus a single DB upsert, so the dashboard's Account panel
shows real numbers. This is NOT reconciliation, scheduling, the rules engine, or
the order path -- none of those are implemented or invoked here.

Account selection
-----------------
The app key can see several accounts (CASH + MARGIN). The correct, production
behaviour is to operate on exactly ONE account, pinned via `WEBULL_ACCOUNT_ID`
(`settings.webull_account_id`) -- a deliberate owner decision, never a guess.

When that pin is unset, a DEV-ONLY fallback picks the first CASH account that
returns a *complete* balance. This exists only to surface a real number in dev:
the Webull sandbox exposes several canned demo accounts, returned in a
non-deterministic order and with differing response shapes (some CASH accounts
omit net-liquidation/cash; some expose buying power under different keys), so
"which account" is not stable across runs. Before real trading the account MUST
be pinned. Fail closed everywhere: an incomplete/ambiguous balance is never
written as if it were real.

All Webull SDK access stays behind :class:`WebullClient` (the one choke point);
paper vs live is driven entirely off `settings.webull_env` (never hardcoded) and
surfaces as `is_paper` on the stored row.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import structlog

from app.core.config import load_settings
from app.core.db import Database
from app.core.webull import AccountSnapshotRequest, WebullClient

if TYPE_CHECKING:
    from app.core.config import Settings
    from app.core.models import EquitySnapshot
    from app.core.webull import AccountBalance, AccountInfo

log = structlog.get_logger()

# Webull's account_type value for a cash account (vs "MARGIN").
_CASH_ACCOUNT_TYPE = "CASH"


class SnapshotError(Exception):
    """Raised when a usable equity snapshot cannot be produced (fail closed).

    A partial/uncertain balance must never be written as if it were real, and an
    ambiguous account selection must never be guessed for trading -- both raise
    this rather than storing a misleading row.
    """


def _mask_account_id(account_id: str) -> str:
    """Last-4 mask for logs -- never emit a full Webull account id/number."""
    tail = account_id[-4:]
    return f"***{tail}" if len(account_id) >= 4 else "***"


def _is_complete(balance: AccountBalance) -> bool:
    """True only if every NOT NULL `equity_snapshots` figure is present.

    The row needs account_equity (net_liquidation), cash_balance (total_cash) and
    buying_power -- all three columns are NOT NULL -- so a balance missing any of
    them cannot produce a valid row and must not be selected/written.
    """
    return (
        balance.net_liquidation is not None
        and balance.total_cash is not None
        and balance.buying_power is not None
    )


def _read_balance(client: WebullClient, account: AccountInfo) -> AccountBalance:
    return client.get_account_snapshot(
        AccountSnapshotRequest(account_id=account.account_id)
    ).balance


def _resolve_account_balance(
    client: WebullClient, pinned_account_id: str | None
) -> tuple[AccountInfo, AccountBalance]:
    """Pick the account to snapshot and return it with its balance (all sync).

    Pinned (production): read exactly `pinned_account_id`; fail closed if it is
    not among the visible accounts or its balance is incomplete.

    Unpinned (dev placeholder): read CASH accounts in the order Webull returns
    them and use the first with a complete balance; fail closed if none qualifies.
    """
    accounts = client.list_accounts()
    if not accounts:
        raise SnapshotError("Webull returned no accounts to snapshot")

    if pinned_account_id:
        matches = [a for a in accounts if a.account_id == pinned_account_id]
        if not matches:
            raise SnapshotError(
                "configured WEBULL_ACCOUNT_ID is not among the visible accounts"
            )
        account = matches[0]
        balance = _read_balance(client, account)
        if not _is_complete(balance):
            raise SnapshotError(
                "pinned account returned an incomplete balance "
                "(net_liquidation/total_cash/buying_power)"
            )
        return account, balance

    cash_accounts = [
        a for a in accounts if (a.account_type or "").upper() == _CASH_ACCOUNT_TYPE
    ]
    if not cash_accounts:
        raise SnapshotError(
            "no CASH account among Webull accounts; pin the bot's account via "
            "WEBULL_ACCOUNT_ID"
        )
    for account in cash_accounts:
        balance = _read_balance(client, account)
        if _is_complete(balance):
            return account, balance
        log.warning(
            "snapshot.account_incomplete_balance",
            account_id_masked=_mask_account_id(account.account_id),
            account_type=account.account_type,
        )
    raise SnapshotError(
        "no CASH account returned a complete balance; pin the bot's account via "
        "WEBULL_ACCOUNT_ID"
    )


async def take_snapshot(
    *,
    settings: Settings | None = None,
    client: WebullClient | None = None,
    db: Database | None = None,
) -> EquitySnapshot:
    """Read the (paper) Webull account balance and store one equity snapshot.

    Loads config, builds the read-only :class:`WebullClient`, resolves the
    snapshot account (see module docstring), reads its balance, maps the fields,
    and upserts one `equity_snapshots` row for today (UTC) -- returning the stored
    :class:`EquitySnapshot`.

    `settings`, `client`, and `db` are injectable for testing; in production all
    three default to real instances built from the environment. When this function
    opens the DB pool itself it closes it before returning; an injected `db` is
    left to the caller to manage.

    The blocking SDK calls run in a worker thread so they never stall the event
    loop. Fails closed: an unusable balance raises :class:`SnapshotError` (never
    writes a partial row); any DB failure surfaces as
    :class:`app.core.db.DatabaseError`.
    """
    settings = settings or load_settings()
    client = client or WebullClient(settings)

    owns_db = db is None
    if db is None:
        db = await Database.connect(settings.database_url)
    try:
        account, balance = await asyncio.to_thread(
            _resolve_account_balance, client, settings.webull_account_id
        )
        masked = _mask_account_id(account.account_id)
        is_paper = not client.is_live
        log.info(
            "snapshot.account_selected",
            account_id_masked=masked,
            account_type=account.account_type,
            pinned=settings.webull_account_id is not None,
            is_paper=is_paper,
        )

        equity = balance.net_liquidation
        cash = balance.total_cash
        buying_power = balance.buying_power
        # _resolve_account_balance already guarantees completeness; re-check here
        # for narrowing + defence in depth (never insert a fabricated/partial row).
        if equity is None or cash is None or buying_power is None:
            raise SnapshotError(
                "resolved balance is incomplete "
                "(net_liquidation/total_cash/buying_power)"
            )

        result = await db.insert_equity_snapshot(
            account_equity=equity,
            cash_balance=cash,
            buying_power=buying_power,
            is_paper=is_paper,
        )
        log.info(
            "snapshot.stored",
            account_id_masked=masked,
            snapshot_date=result.snapshot_date.isoformat(),
            account_equity=str(result.account_equity),
            cash_balance=str(result.cash_balance),
            buying_power=str(result.buying_power),
            is_paper=result.is_paper,
        )
        return result
    finally:
        if owns_db:
            await db.close()
