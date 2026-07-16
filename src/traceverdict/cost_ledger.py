"""Auditable cross-currency helpers for the API cost ledger."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class EcbEurReferenceRates:
    """ECB reference quotes expressed as units of currency per one euro."""

    usd_per_one_eur: Decimal
    cny_per_one_eur: Decimal

    @property
    def usd_per_one_cny(self) -> Decimal:
        return self.usd_per_one_eur / self.cny_per_one_eur

    @property
    def cny_per_one_usd(self) -> Decimal:
        return self.cny_per_one_eur / self.usd_per_one_eur

    def cny_to_usd(self, value: Decimal | str) -> Decimal:
        return Decimal(value) * self.usd_per_one_cny


ECB_2026_07_10 = EcbEurReferenceRates(
    usd_per_one_eur=Decimal("1.1430"),
    cny_per_one_eur=Decimal("7.7433"),
)


def corrected_cny_entry(
    *, amount_cny: Decimal | str, old_usd: Decimal | str | None = None
) -> dict[str, str | None]:
    """Return an append-only ledger correction without mutating source evidence."""
    amount = Decimal(amount_cny)
    corrected = ECB_2026_07_10.cny_to_usd(amount)
    previous = None if old_usd is None else Decimal(old_usd)
    return {
        "original_currency": "CNY",
        "original_amount": str(amount),
        "old_usd": None if previous is None else str(previous),
        "corrected_usd": str(corrected),
        "delta_usd": None if previous is None else str(corrected - previous),
    }
