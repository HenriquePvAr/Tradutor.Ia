"""Provider-neutral ayeT rewarded callback verification primitives.

The production boundary is deliberately fail-closed: no client payload can
mint YK, and callbacks without a configured signing secret, transaction id,
or valid HMAC are rejected before the credit RPC is reached.
"""
from __future__ import annotations

import hashlib
import hmac
from urllib.parse import urlencode
from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True, slots=True)
class AyetRewardEvent:
    external_identifier: str
    transaction_id: str
    amount: int
    adslot_id: str


def canonical_callback_payload(params: Mapping[str, str]) -> str:
    """Stable payload for local HMAC verification.

    ayeT's dashboard callback format is configurable; the release adapter uses
    the documented transaction/external id/amount/adslot tuple and requires the
    account's callback signing contract to match this canonical form.
    """
    return urlencode(sorted((str(k), str(v)) for k, v in params.items() if str(k) != "signature"))


def verify_callback(params: Mapping[str, str], signature: str, secret: str) -> AyetRewardEvent:
    if not secret:
        raise ValueError("provider_not_configured")
    external = str(params.get("external_identifier", "")).strip()
    transaction = str(params.get("transaction_id", "")).strip()
    adslot = str(params.get("adslot_id", "")).strip()
    if not external or not transaction or not adslot:
        raise ValueError("invalid_reward_callback")
    try:
        amount = int(params.get("amount", "0"))
    except (TypeError, ValueError):
        raise ValueError("invalid_reward_callback") from None
    if amount != 1:
        raise ValueError("invalid_reward_callback")
    expected = hmac.new(secret.encode(), canonical_callback_payload(params).encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, str(signature or "").strip().lower()):
        raise ValueError("invalid_reward_signature")
    return AyetRewardEvent(external, transaction, amount, adslot)
