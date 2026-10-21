"""Single-sale Etsy pricing in SEK.

Customer-facing target (Sweden): 230 + 20R + 10S whole kronor, VAT included.
Etsy digital downloads list a net price; for EU buyers Etsy adds local VAT on
top. There is no shop toggle for “prices include VAT” on digital items. We send
net = inclusive × 4/5 so a Swedish buyer (25% VAT) sees the whole-krona total.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass

from anskar.etsy.errors import EtsyError

BASE_SEK = 230
R_WEIGHT = 20
S_WEIGHT = 10
DISCOUNT_FRACTION = 0.20
REQUIRED_CURRENCY = "SEK"
OFFERING_DAYS = 42
DISCOUNT_AFTER_DAYS = 30
# Swedish digital VAT rate Etsy applies for SE buyers viewing digital listings.
SWEDISH_DIGITAL_VAT = 0.25
PRICE_INCLUDES_VAT = True
VAT_INCLUSIVE_NOTE = (
    "Price shown to buyers in Sweden is a whole number of kronor including VAT. "
    "Etsy adds local digital VAT for EU buyers; the net listing price is set so "
    "the Swedish total matches this amount."
)


@dataclass(frozen=True)
class ArtworkPrice:
    r: int
    s: int
    original_sek: int
    currency: str = REQUIRED_CURRENCY

    @property
    def discounted_sek(self) -> int:
        """20% off, snapped to a clean Swedish VAT-inclusive whole krona."""
        raw = int(round(self.original_sek * (1.0 - DISCOUNT_FRACTION)))
        return _swedish_inclusive_sek(raw)

    def as_api_price(self, *, discounted: bool = False) -> float:
        """
        Net SEK for createDraftListing / updateListing.

        Etsy adds EU digital VAT for EU buyers. Net = inclusive × 4/5 so Sweden
        (25%) displays the intended whole-krona inclusive price.
        """
        inclusive = self.discounted_sek if discounted else self.original_sek
        return float(_net_sek_for_swedish_inclusive(inclusive))


def _swedish_inclusive_sek(sek: int) -> int:
    """Nearest whole SEK that is a clean 25% VAT total (multiple of 5)."""
    return int(round(sek / 5.0) * 5)


def _net_sek_for_swedish_inclusive(inclusive_sek: int) -> int:
    if inclusive_sek % 5 != 0:
        raise EtsyError(
            f"Inclusive SEK {inclusive_sek} is not a clean Swedish 25% VAT total.",
            kind="credentials",
        )
    return inclusive_sek * 4 // 5


def choose_r(*, rng=None) -> int:
    """Random integer 0–9, selected once per artwork."""
    if rng is None:
        return secrets.randbelow(10)
    return int(rng.randrange(0, 10))


def calculate_price(*, sales_before: int, r: int | None = None, rng=None) -> ArtworkPrice:
    """
    Intended Swedish storefront total in whole SEK (VAT included via Etsy's add-on).

    The API receives the lower net amount from ArtworkPrice.as_api_price().
    """
    if sales_before < 0:
        raise EtsyError("Completed sales count cannot be negative.", kind="credentials")
    chosen = choose_r(rng=rng) if r is None else int(r)
    if chosen < 0 or chosen > 9:
        raise EtsyError("Price factor R must be an integer from 0 through 9.", kind="credentials")
    s = int(sales_before)
    original = _swedish_inclusive_sek(BASE_SEK + R_WEIGHT * chosen + S_WEIGHT * s)
    return ArtworkPrice(r=chosen, s=s, original_sek=original, currency=REQUIRED_CURRENCY)


def assert_shop_currency(currency_code: str | None) -> str:
    code = (currency_code or "").strip().upper()
    if code != REQUIRED_CURRENCY:
        raise EtsyError(
            f"Etsy shop currency is {currency_code!r}; Anskar only lists in {REQUIRED_CURRENCY}. "
            "Fix the shop currency before activating listings.",
            kind="credentials",
        )
    return code
