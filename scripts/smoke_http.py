"""HTTP smoke tests for POST /api/x12/audit.

Exercises the running API with both a valid envelope and a series of
damaged envelopes, asserting on HTTP status, stable error codes and the
first locatable segment index.  When ``X12_PARTNER_PROFILES`` is set, an
additional block covers the partner-profile feature: header-less
compatibility, a matching profile, an unknown profile and mismatches at
interchange/group/transaction scope.  Exits non-zero if any assertion
fails.

Usage: python3 smoke_http.py [BASE_URL]
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import urllib.error
import urllib.request

BASE_URL = (
    sys.argv[1]
    if len(sys.argv) > 1
    else os.environ.get("BASE_URL", "http://localhost:8080")
).rstrip("/")

ENDPOINT = f"{BASE_URL}/api/x12/audit"
MAX_BYTES = 2 * 1024 * 1024  # 2 MiB

failures: list[str] = []


def isa(
    control: str = "000000001",
    sender_qual: str = "ZZ",
    sender: str = "SENDER",
    receiver_qual: str = "ZZ",
    receiver: str = "PARTNER",
) -> str:
    fields = [
        "00",
        " " * 10,
        "00",
        " " * 10,
        sender_qual.ljust(2),
        sender.ljust(15),
        receiver_qual.ljust(2),
        receiver.ljust(15),
        " " * 6,
        " " * 4,
        "U",
        "00501",
        control.rjust(9),
        "0",
        "P",
        ":",
    ]
    return "ISA*" + "*".join(fields) + "~"


GS = "GS*PO*SENDER*PARTNER*20240101*1200*1*X*005010~"
IEA = "IEA*1*000000001~"


def post(
    raw: bytes,
    content_type: str = "application/octet-stream",
    profile: str | None = None,
):
    headers = {"Content-Type": content_type}
    if profile is not None:
        headers["X-Partner-Profile"] = profile
    req = urllib.request.Request(ENDPOINT, data=raw, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        failures.append(name)


def scenario_valid():
    print("scenario: valid multi-group interchange")
    body = (
        isa()
        + "GS*PO*S*R*D*T*1*X*V~"
        + "ST*850*100~BEG*00~REF*A:B~SE*4*100~"
        + "GE*1*1~"
        + "GS*PO*S*R*D*T*2*X*V~"
        + "ST*850*200~SE*2*200~"
        + "ST*850*201~SE*2*201~"
        + "GE*2*2~"
        + "IEA*2*000000001~"
    ).encode("ascii")
    status, payload = post(body)
    check("http 200", status == 200, f"got {status} {payload}")
    check(
        "control number",
        payload.get("interchange_control_number") == "000000001",
        str(payload),
    )
    check("group count", payload.get("group_count") == 2, str(payload))
    check("transaction count", payload.get("transaction_count") == 3, str(payload))
    check(
        "sha256 of raw body",
        payload.get("sha256") == hashlib.sha256(body).hexdigest(),
        str(payload),
    )


def expect_envelope_error(name, body: bytes, status_code: int, code: str, segment: int):
    print(f"scenario: {name}")
    status, payload = post(body)
    error = payload.get("error", {})
    check("http status", status == status_code, f"got {status} {payload}")
    check("error code", error.get("code") == code, str(payload))
    check("segment index", error.get("segment") == segment, str(payload))
    check("message present", bool(error.get("message")), str(payload))


def scenario_damaged():
    expect_envelope_error(
        "truncated final segment (no terminator)",
        (isa() + GS + "ST*850*100~SE*2*100~GE*1*1~IEA*1*000000001").encode(),
        422,
        "MISSING_TERMINATOR",
        6,
    )

    # Inner SE error plus deliberately wrong GE and IEA summaries: the
    # innermost, earliest error must not be masked.
    expect_envelope_error(
        "SE count wrong while GE/IEA summaries also wrong",
        (
            isa()
            + GS
            + "ST*850*100~BEG*00~SE*2*100~"   # actual span is 3
            + "GE*9*1~"
            + "IEA*9*000000001~"
        ).encode("ascii"),
        422,
        "SEGMENT_COUNT_MISMATCH",
        5,
    )

    expect_envelope_error(
        "SE control number mismatch",
        (isa() + GS + "ST*850*100~SE*2*999~GE*1*1~" + IEA).encode("ascii"),
        422,
        "CONTROL_NUMBER_MISMATCH",
        4,
    )

    expect_envelope_error(
        "GE transaction count mismatch",
        (
            isa()
            + GS
            + "ST*850*100~SE*2*100~"
            + "ST*850*200~SE*2*200~"
            + "GE*1*1~" + IEA
        ).encode("ascii"),
        422,
        "GE_COUNT_MISMATCH",
        7,
    )

    expect_envelope_error(
        "IEA group count mismatch",
        (isa() + GS + "ST*850*100~SE*2*100~GE*1*1~IEA*2*000000001~").encode(),
        422,
        "IEA_COUNT_MISMATCH",
        6,
    )

    expect_envelope_error(
        "IEA control number mismatch",
        (isa() + GS + "ST*850*100~SE*2*100~GE*1*1~IEA*1*000000999~").encode(),
        422,
        "CONTROL_NUMBER_MISMATCH",
        6,
    )

    expect_envelope_error(
        "interleaved levels (GE before SE)",
        (isa() + GS + "ST*850*100~GE*1*1~" + IEA).encode("ascii"),
        422,
        "NESTING_VIOLATION",
        4,
    )

    expect_envelope_error(
        "trailing data after IEA",
        (isa() + GS + "ST*850*100~SE*2*100~GE*1*1~" + IEA + "ZZZ*1~").encode(),
        422,
        "TRAILING_DATA",
        7,
    )

    expect_envelope_error(
        "second ISA in one message",
        (isa() + isa()).encode("ascii"),
        422,
        "MULTIPLE_INTERCHANGES",
        2,
    )


def scenario_transport():
    print("scenario: wrong content type")
    status, payload = post(b"whatever", "text/plain")
    check("http 415", status == 415, f"got {status} {payload}")
    check(
        "error code",
        payload.get("error", {}).get("code") == "UNSUPPORTED_MEDIA_TYPE",
        str(payload),
    )

    print("scenario: empty body")
    status, payload = post(b"")
    check("http 400", status == 400, f"got {status} {payload}")
    check(
        "error code",
        payload.get("error", {}).get("code") == "EMPTY_MESSAGE",
        str(payload),
    )

    print("scenario: body over 2 MiB")
    status, payload = post(b"x" * (MAX_BYTES + 1))
    check("http 413", status == 413, f"got {status} {payload}")
    check(
        "error code",
        payload.get("error", {}).get("code") == "MESSAGE_TOO_LARGE",
        str(payload),
    )

    print("scenario: health endpoint")
    with urllib.request.urlopen(f"{BASE_URL}/health", timeout=5) as response:
        check("health 200", response.status == 200)


def scenario_profiles():
    raw_cfg = os.environ.get("X12_PARTNER_PROFILES", "")
    if not raw_cfg.strip():
        print("scenario: partner profiles — skipped (X12_PARTNER_PROFILES not set)")
        return
    profile = json.loads(raw_cfg)[0]
    contract = profile["groups"][0]
    name = profile["name"]
    allowed = contract["st01"]

    def build(*, isa06=None, gs01=None, gs02=None, gs03=None, gs08=None, st01=None):
        return (
            isa(
                sender_qual=profile["isa05"],
                sender=isa06 if isa06 is not None else profile["isa06"],
                receiver_qual=profile["isa07"],
                receiver=profile["isa08"],
            )
            + "GS*{}*{}*{}*20240101*1200*1*X*{}~".format(
                gs01 if gs01 is not None else contract["gs01"],
                gs02 if gs02 is not None else profile["gs02"],
                gs03 if gs03 is not None else profile["gs03"],
                gs08 if gs08 is not None else contract["gs08"],
            )
            + "ST*{}*100~SE*2*100~".format(
                st01 if st01 is not None else allowed[0]
            )
            + "GE*1*1~IEA*1*000000001~"
        ).encode("ascii")

    print("scenario: profile match")
    body = build()
    status, payload = post(body, profile=name)
    check("http 200", status == 200, f"got {status} {payload}")
    check(
        "control number",
        payload.get("interchange_control_number") == "000000001",
        str(payload),
    )
    check("group count", payload.get("group_count") == 1, str(payload))
    check("transaction count", payload.get("transaction_count") == 1, str(payload))
    check(
        "sha256 of raw body",
        payload.get("sha256") == hashlib.sha256(body).hexdigest(),
        str(payload),
    )

    print("scenario: unknown partner profile")
    status, payload = post(build(), profile="no-such-profile")
    error = payload.get("error", {})
    check("http 400", status == 400, f"got {status} {payload}")
    check("error code", error.get("code") == "PROFILE_NOT_FOUND", str(payload))

    print("scenario: profile mismatch at interchange (ISA06)")
    status, payload = post(build(isa06="INTRUDER"), profile=name)
    error = payload.get("error", {})
    check("http 422", status == 422, f"got {status} {payload}")
    check("error code", error.get("code") == "PROFILE_MISMATCH", str(payload))
    check("scope", error.get("scope") == "interchange", str(payload))
    check("segment index", error.get("segment") == 1, str(payload))

    print("scenario: profile mismatch at group (uncontracted GS01)")
    used_gs01 = {c["gs01"] for c in profile["groups"]}
    free_gs01 = next(c for c in ("ZZ", "XX", "YY") if c not in used_gs01)
    status, payload = post(build(gs01=free_gs01), profile=name)
    error = payload.get("error", {})
    check("http 422", status == 422, f"got {status} {payload}")
    check("error code", error.get("code") == "PROFILE_MISMATCH", str(payload))
    check("scope", error.get("scope") == "group", str(payload))
    check("segment index", error.get("segment") == 2, str(payload))

    print("scenario: profile mismatch at transaction (ST01 not allowed)")
    free_st01 = next(c for c in ("999", "998", "997") if c not in allowed)
    status, payload = post(build(st01=free_st01), profile=name)
    error = payload.get("error", {})
    check("http 422", status == 422, f"got {status} {payload}")
    check("error code", error.get("code") == "PROFILE_MISMATCH", str(payload))
    check("scope", error.get("scope") == "transaction", str(payload))
    check("segment index", error.get("segment") == 3, str(payload))

    print("scenario: envelope error outranks profile check")
    damaged = (
        isa(
            sender_qual=profile["isa05"],
            sender=profile["isa06"],
            receiver_qual=profile["isa07"],
            receiver=profile["isa08"],
        )
        + "GS*{}*{}*{}*20240101*1200*1*X*{}~".format(
            contract["gs01"], profile["gs02"], profile["gs03"], contract["gs08"]
        )
        # SE01 declares 2 but the ST..SE span is 3: the structural error
        # must surface, not a profile result.
        + "ST*{}*100~BEG*00~SE*2*100~".format(allowed[0])
        + "GE*1*1~IEA*1*000000001~"
    ).encode("ascii")
    status, payload = post(damaged, profile=name)
    error = payload.get("error", {})
    check("http 422", status == 422, f"got {status} {payload}")
    check(
        "error code", error.get("code") == "SEGMENT_COUNT_MISMATCH", str(payload)
    )
    check("segment index", error.get("segment") == 5, str(payload))


def main() -> int:
    print(f"Smoke testing X12 audit API at {ENDPOINT}")
    scenario_valid()
    scenario_damaged()
    scenario_transport()
    scenario_profiles()
    print()
    if failures:
        print(f"{len(failures)} smoke check(s) FAILED: {', '.join(failures)}")
        return 1
    print("All HTTP smoke checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
