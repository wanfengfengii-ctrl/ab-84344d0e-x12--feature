"""Partner profile configuration and X12 identity matching.

A profile is the envelope identity agreed with one trading partner:

* interchange parties  — ISA05/ISA06 (sender qualifier/id) and
  ISA07/ISA08 (receiver qualifier/id),
* group parties        — GS02 (application sender code) and GS03
  (application receiver code), shared by every group,
* one to eight group contracts, each declaring:
    - ``GS01``  functional identifier code,
    - ``GS08``  version / release / industry identifier code,
    - ``ST01``  allowed transaction set identifier codes (one to eight).

Profiles are supplied as a JSON array through the ``X12_PARTNER_PROFILES``
environment variable (one to sixteen profiles).  Example::

    [
      {
        "name": "acme",
        "ISA05": "ZZ", "ISA06": "ACME",
        "ISA07": "ZZ", "ISA08": "PLATFORM",
        "GS02": "ACME", "GS03": "PLATFORM",
        "groups": [
          {"GS01": "PO", "GS08": "005010", "ST01": ["850"]}
        ]
      }
    ]

Identity comparison rules follow the X12 envelope conventions: ISA05..08
are fixed-width fields, so the auditor strips only right-hand space
padding before comparison; leading spaces and letter case stay
significant.  GS/ST identifiers are compared verbatim and
case-sensitively.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

from .audit import EnvelopeView

MAX_PROFILES = 16
MAX_GROUP_CONTRACTS = 8
MAX_TRANSACTION_SET_IDS = 8

# X12 standard maximum field lengths; values longer than these could
# never appear in a legal envelope, so they are configuration errors.
LEN_ISA_QUALIFIER = 2
LEN_ISA_ID = 15
LEN_GS01 = 2
LEN_GS_PARTY = 15
LEN_GS08 = 12
LEN_ST01 = 3

ENV_VAR = "X12_PARTNER_PROFILES"

# Where a mismatch was detected; the first violating segment is reported
# alongside (ISA always lives at segment 1).
SCOPE_INTERCHANGE = "interchange"
SCOPE_GROUP = "group"
SCOPE_TRANSACTION = "transaction"


class ProfileConfigError(ValueError):
    """Raised at startup when ``X12_PARTNER_PROFILES`` is invalid."""


@dataclass(frozen=True)
class GroupContract:
    functional_identifier: str
    version: str
    transaction_set_ids: frozenset[str]


@dataclass(frozen=True)
class PartnerProfile:
    name: str
    sender_qualifier: str
    sender_id: str
    receiver_qualifier: str
    receiver_id: str
    application_sender: str
    application_receiver: str
    group_contracts: tuple[GroupContract, ...]


class ProfileMismatch(Exception):
    """Raised when a structurally valid envelope violates a profile.

    ``scope`` is one of ``interchange`` / ``group`` / ``transaction`` and
    ``segment`` is the 1-based index of the first violating segment.
    """

    def __init__(self, scope: str, message: str, segment: int):
        super().__init__(message)
        self.scope = scope
        self.segment = segment


def _field(obj: dict, name: str, path: str) -> str:
    if name not in obj:
        raise ProfileConfigError(f"{path}.{name} is required")
    value = obj[name]
    if not isinstance(value, str):
        raise ProfileConfigError(f"{path}.{name} must be a string")
    if not value:
        raise ProfileConfigError(f"{path}.{name} must be a non-empty string")
    if not value.isascii():
        raise ProfileConfigError(
            f"{path}.{name} must contain ASCII characters only"
        )
    return value


def _bounded(value: str, max_len: int, path: str) -> str:
    if len(value) > max_len:
        raise ProfileConfigError(f"{path} must be at most {max_len} characters")
    return value


def _parse_group_contract(obj: object, path: str) -> GroupContract:
    if not isinstance(obj, dict):
        raise ProfileConfigError(f"{path} must be an object")
    gs01 = _bounded(_field(obj, "GS01", path), LEN_GS01, f"{path}.GS01")
    gs08 = _bounded(_field(obj, "GS08", path), LEN_GS08, f"{path}.GS08")
    if "ST01" not in obj:
        raise ProfileConfigError(f"{path}.ST01 is required")
    raw_ids = obj["ST01"]
    if not isinstance(raw_ids, list) or not raw_ids:
        raise ProfileConfigError(
            f"{path}.ST01 must be a non-empty array of strings"
        )
    if len(raw_ids) > MAX_TRANSACTION_SET_IDS:
        raise ProfileConfigError(
            f"{path}.ST01 may contain at most {MAX_TRANSACTION_SET_IDS} codes"
        )
    ids: set[str] = set()
    for index, raw_id in enumerate(raw_ids):
        item_path = f"{path}.ST01[{index}]"
        if not isinstance(raw_id, str) or not raw_id:
            raise ProfileConfigError(
                f"{item_path} must be a non-empty string"
            )
        if not raw_id.isascii():
            raise ProfileConfigError(
                f"{item_path} must contain ASCII characters only"
            )
        _bounded(raw_id, LEN_ST01, item_path)
        if raw_id in ids:
            raise ProfileConfigError(f"{item_path} duplicates {raw_id!r}")
        ids.add(raw_id)
    unknown = set(obj) - {"GS01", "GS08", "ST01"}
    if unknown:
        raise ProfileConfigError(
            f"{path} contains unknown field(s): {', '.join(sorted(unknown))}"
        )
    return GroupContract(
        functional_identifier=gs01,
        version=gs08,
        transaction_set_ids=frozenset(ids),
    )


def _parse_profile(obj: object, index: int) -> PartnerProfile:
    path = f"$[{index}]"
    if not isinstance(obj, dict):
        raise ProfileConfigError(f"{path} must be an object")
    name = _field(obj, "name", path)
    isa05 = _bounded(
        _field(obj, "ISA05", path), LEN_ISA_QUALIFIER, f"{path}.ISA05"
    )
    isa06 = _bounded(_field(obj, "ISA06", path), LEN_ISA_ID, f"{path}.ISA06")
    isa07 = _bounded(
        _field(obj, "ISA07", path), LEN_ISA_QUALIFIER, f"{path}.ISA07"
    )
    isa08 = _bounded(_field(obj, "ISA08", path), LEN_ISA_ID, f"{path}.ISA08")
    gs02 = _bounded(_field(obj, "GS02", path), LEN_GS_PARTY, f"{path}.GS02")
    gs03 = _bounded(_field(obj, "GS03", path), LEN_GS_PARTY, f"{path}.GS03")
    if "groups" not in obj:
        raise ProfileConfigError(f"{path}.groups is required")
    raw_groups = obj["groups"]
    if not isinstance(raw_groups, list) or not raw_groups:
        raise ProfileConfigError(f"{path}.groups must be a non-empty array")
    if len(raw_groups) > MAX_GROUP_CONTRACTS:
        raise ProfileConfigError(
            f"{path}.groups may contain at most {MAX_GROUP_CONTRACTS} contracts"
        )
    contracts: list[GroupContract] = []
    seen: set[tuple[str, str]] = set()
    for g_index, raw_group in enumerate(raw_groups):
        contract = _parse_group_contract(raw_group, f"{path}.groups[{g_index}]")
        key = (contract.functional_identifier, contract.version)
        if key in seen:
            raise ProfileConfigError(
                f"{path}.groups[{g_index}] duplicates the GS01/GS08 pair "
                f"{key[0]!r}/{key[1]!r}"
            )
        seen.add(key)
        contracts.append(contract)
    unknown = set(obj) - {
        "name",
        "ISA05",
        "ISA06",
        "ISA07",
        "ISA08",
        "GS02",
        "GS03",
        "groups",
    }
    if unknown:
        raise ProfileConfigError(
            f"{path} contains unknown field(s): {', '.join(sorted(unknown))}"
        )
    return PartnerProfile(
        name=name,
        sender_qualifier=isa05,
        sender_id=isa06,
        receiver_qualifier=isa07,
        receiver_id=isa08,
        application_sender=gs02,
        application_receiver=gs03,
        group_contracts=tuple(contracts),
    )


def parse_profiles_json(text: str) -> dict[str, PartnerProfile]:
    """Parse and validate the ``X12_PARTNER_PROFILES`` JSON text."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProfileConfigError(f"{ENV_VAR} is not valid JSON: {exc}") from None
    if not isinstance(data, list) or not data:
        raise ProfileConfigError(f"{ENV_VAR} must be a non-empty JSON array")
    if len(data) > MAX_PROFILES:
        raise ProfileConfigError(
            f"{ENV_VAR} may define at most {MAX_PROFILES} profiles"
        )
    profiles: dict[str, PartnerProfile] = {}
    for index, entry in enumerate(data):
        profile = _parse_profile(entry, index)
        if profile.name in profiles:
            raise ProfileConfigError(
                f"$[{index}].name {profile.name!r} duplicates an earlier profile"
            )
        profiles[profile.name] = profile
    return profiles


def load_profiles_from_env(
    env: dict[str, str] | None = None,
) -> dict[str, PartnerProfile] | None:
    """Load profiles from the environment.

    Returns ``None`` when the variable is absent or blank (partner
    profiling is disabled).  Raises :class:`ProfileConfigError` for any
    other value.
    """
    if env is None:
        env = os.environ
    text = env.get(ENV_VAR)
    if text is None or not text.strip():
        return None
    return parse_profiles_json(text)


def match(profile: PartnerProfile, view: EnvelopeView) -> None:
    """Verify an audited envelope against one profile.

    Checks run strictly in message order — interchange parties first,
    then each functional group (party codes before its contract) and
    every transaction inside it — so the raised :class:`ProfileMismatch`
    names the first violating segment.
    """
    name = profile.name

    if (
        view.sender_qualifier != profile.sender_qualifier
        or view.sender_id != profile.sender_id
    ):
        raise ProfileMismatch(
            SCOPE_INTERCHANGE,
            f"ISA sender {view.sender_qualifier!r}/{view.sender_id!r} does "
            f"not match profile {name!r} sender "
            f"{profile.sender_qualifier!r}/{profile.sender_id!r}",
            1,
        )
    if (
        view.receiver_qualifier != profile.receiver_qualifier
        or view.receiver_id != profile.receiver_id
    ):
        raise ProfileMismatch(
            SCOPE_INTERCHANGE,
            f"ISA receiver {view.receiver_qualifier!r}/{view.receiver_id!r} "
            f"does not match profile {name!r} receiver "
            f"{profile.receiver_qualifier!r}/{profile.receiver_id!r}",
            1,
        )

    for group in view.groups:
        if group.application_sender != profile.application_sender:
            raise ProfileMismatch(
                SCOPE_GROUP,
                f"GS02 {group.application_sender!r} does not match profile "
                f"{name!r} GS02 {profile.application_sender!r}",
                group.gs_index,
            )
        if group.application_receiver != profile.application_receiver:
            raise ProfileMismatch(
                SCOPE_GROUP,
                f"GS03 {group.application_receiver!r} does not match profile "
                f"{name!r} GS03 {profile.application_receiver!r}",
                group.gs_index,
            )
        contract = next(
            (
                candidate
                for candidate in profile.group_contracts
                if candidate.functional_identifier
                == group.functional_identifier
                and candidate.version == group.version
            ),
            None,
        )
        if contract is None:
            raise ProfileMismatch(
                SCOPE_GROUP,
                f"GS01/GS08 {group.functional_identifier!r}/"
                f"{group.version!r} is not contracted with profile {name!r}",
                group.gs_index,
            )
        for transaction in group.transactions:
            if transaction.transaction_set_id not in contract.transaction_set_ids:
                raise ProfileMismatch(
                    SCOPE_TRANSACTION,
                    f"ST01 {transaction.transaction_set_id!r} is not allowed "
                    f"for GS01/GS08 {group.functional_identifier!r}/"
                    f"{group.version!r} in profile {name!r}",
                    transaction.st_index,
                )
