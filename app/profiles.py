"""Partner profile registry and post-audit conformance checks.

``X12_PARTNER_PROFILES`` carries a JSON array of 1..16 unique partner
profiles.  Each profile pins the interchange parties (ISA05/06/07/08),
the application sender/receiver codes (GS02/GS03) and 1..8 group
contracts; a contract pairs a functional identifier (GS01) and a
version/release (GS08) with the set of transaction set ids (ST01) the
group may carry.

When a request selects a profile through the ``X-Partner-Profile``
header, the structural envelope audit runs first; only a valid
interchange is then checked against the profile, in message order: the
interchange parties, then each functional group and all of its
transactions.  ISA fixed-width fields are compared after stripping
right-side padding spaces only; every other identifier is compared
exactly as sent (case-sensitive).
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .audit import EnvelopeDetail

MAX_PROFILES = 16
MAX_GROUP_CONTRACTS = 8

# Fixed widths of the ISA identity fields; a longer configured value
# could never match a syntactically valid ISA segment.
_ISA_QUALIFIER_WIDTH = 2
_ISA_ID_WIDTH = 15


class ProfileConfigError(Exception):
    """Raised when X12_PARTNER_PROFILES cannot be parsed or validated."""


@dataclass(frozen=True)
class GroupContract:
    """One GS01/GS08 pair plus the ST01 values allowed inside it."""

    gs01: str
    gs08: str
    st01: frozenset[str]


@dataclass(frozen=True)
class PartnerProfile:
    name: str
    isa05: str
    isa06: str
    isa07: str
    isa08: str
    gs02: str
    gs03: str
    groups: tuple[GroupContract, ...]


class ProfileMismatch(Exception):
    """First profile violation, located by scope and 1-based segment index."""

    def __init__(self, message: str, scope: str, segment: int):
        super().__init__(message)
        self.scope = scope
        self.segment = segment


def _require_str(value: object, what: str, max_len: int | None = None) -> str:
    if not isinstance(value, str) or not value:
        raise ProfileConfigError(f"{what} must be a non-empty string")
    if not value.isascii():
        raise ProfileConfigError(f"{what} must contain only ASCII characters")
    if max_len is not None and len(value) > max_len:
        raise ProfileConfigError(
            f"{what} must be at most {max_len} characters long"
        )
    return value


def _load_contract(item: object, where: str) -> GroupContract:
    if not isinstance(item, dict):
        raise ProfileConfigError(f"{where} must be a JSON object")
    gs01 = _require_str(item.get("gs01"), f"{where} gs01")
    gs08 = _require_str(item.get("gs08"), f"{where} gs08")
    st01_raw = item.get("st01")
    if not isinstance(st01_raw, list) or not st01_raw:
        raise ProfileConfigError(f"{where} st01 must be a non-empty array")
    st01 = frozenset(
        _require_str(entry, f"{where} st01 entry") for entry in st01_raw
    )
    return GroupContract(gs01=gs01, gs08=gs08, st01=st01)


def load_profiles(raw: str | None) -> dict[str, PartnerProfile]:
    """Parse and validate the ``X12_PARTNER_PROFILES`` JSON document.

    An unset or blank value yields an empty registry (the feature stays
    dormant); a present but malformed document raises
    :class:`ProfileConfigError`.
    """
    if raw is None or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProfileConfigError(f"value is not valid JSON: {exc}") from None
    if not isinstance(data, list):
        raise ProfileConfigError("top level must be an array of profiles")
    if not 1 <= len(data) <= MAX_PROFILES:
        raise ProfileConfigError(
            f"expected 1..{MAX_PROFILES} profiles, got {len(data)}"
        )

    profiles: dict[str, PartnerProfile] = {}
    for index, item in enumerate(data):
        where = f"profile #{index + 1}"
        if not isinstance(item, dict):
            raise ProfileConfigError(f"{where} must be a JSON object")
        name = _require_str(item.get("name"), f"{where} name")
        if name in profiles:
            raise ProfileConfigError(f"duplicate profile name {name!r}")

        groups_raw = item.get("groups")
        if not isinstance(groups_raw, list) or not (
            1 <= len(groups_raw) <= MAX_GROUP_CONTRACTS
        ):
            raise ProfileConfigError(
                f"{where} groups must be an array of "
                f"1..{MAX_GROUP_CONTRACTS} contracts"
            )
        contracts: list[GroupContract] = []
        seen_pairs: set[tuple[str, str]] = set()
        for c_index, c_item in enumerate(groups_raw):
            contract = _load_contract(
                c_item, f"{where} contract #{c_index + 1}"
            )
            pair = (contract.gs01, contract.gs08)
            if pair in seen_pairs:
                raise ProfileConfigError(
                    f"{where} duplicates the GS01 {pair[0]!r} / "
                    f"GS08 {pair[1]!r} contract"
                )
            seen_pairs.add(pair)
            contracts.append(contract)

        profiles[name] = PartnerProfile(
            name=name,
            isa05=_require_str(
                item.get("isa05"), f"{where} isa05", _ISA_QUALIFIER_WIDTH
            ),
            isa06=_require_str(
                item.get("isa06"), f"{where} isa06", _ISA_ID_WIDTH
            ),
            isa07=_require_str(
                item.get("isa07"), f"{where} isa07", _ISA_QUALIFIER_WIDTH
            ),
            isa08=_require_str(
                item.get("isa08"), f"{where} isa08", _ISA_ID_WIDTH
            ),
            gs02=_require_str(item.get("gs02"), f"{where} gs02"),
            gs03=_require_str(item.get("gs03"), f"{where} gs03"),
            groups=tuple(contracts),
        )
    return profiles


def check_envelope_against_profile(
    envelope: EnvelopeDetail, profile: PartnerProfile
) -> None:
    """Raise ProfileMismatch at the first violation, in message order."""
    for element, actual, expected in (
        ("ISA05", envelope.isa05, profile.isa05),
        ("ISA06", envelope.isa06, profile.isa06),
        ("ISA07", envelope.isa07, profile.isa07),
        ("ISA08", envelope.isa08, profile.isa08),
    ):
        # ISA identity fields are fixed-width and right-padded with
        # spaces; only that padding is ignored, case still matters.
        if actual.rstrip(" ") != expected:
            raise ProfileMismatch(
                f"{element} {actual.rstrip(' ')!r} does not match profile "
                f"{profile.name!r} ({expected!r})",
                "interchange",
                1,
            )

    for group in envelope.groups:
        if group.gs02 != profile.gs02:
            raise ProfileMismatch(
                f"GS02 {group.gs02!r} does not match profile "
                f"{profile.name!r} ({profile.gs02!r})",
                "group",
                group.segment,
            )
        if group.gs03 != profile.gs03:
            raise ProfileMismatch(
                f"GS03 {group.gs03!r} does not match profile "
                f"{profile.name!r} ({profile.gs03!r})",
                "group",
                group.segment,
            )
        contract = next(
            (
                c
                for c in profile.groups
                if c.gs01 == group.gs01 and c.gs08 == group.gs08
            ),
            None,
        )
        if contract is None:
            raise ProfileMismatch(
                f"GS01/GS08 {group.gs01!r}/{group.gs08!r} matches no "
                f"group contract of profile {profile.name!r}",
                "group",
                group.segment,
            )
        for txn in group.transactions:
            if txn.st01 not in contract.st01:
                raise ProfileMismatch(
                    f"ST01 {txn.st01!r} is not allowed by the "
                    f"{contract.gs01!r}/{contract.gs08!r} contract of "
                    f"profile {profile.name!r}",
                    "transaction",
                    txn.segment,
                )
