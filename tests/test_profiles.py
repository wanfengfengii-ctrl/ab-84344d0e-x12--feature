"""Unit tests for partner profile loading, matching and HTTP wiring."""

from __future__ import annotations

import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

from app import server
from app.audit import audit
from app.profiles import (
    MAX_GROUP_CONTRACTS,
    MAX_PROFILES,
    ProfileConfigError,
    ProfileMismatch,
    check_envelope_against_profile,
    load_profiles,
)

PROFILE = {
    "name": "acme",
    "isa05": "ZZ",
    "isa06": "SENDER",
    "isa07": "ZZ",
    "isa08": "PARTNER",
    "gs02": "SENDERAPP",
    "gs03": "PARTNERAPP",
    "groups": [
        {"gs01": "PO", "gs08": "005010", "st01": ["850", "855"]},
        {"gs01": "IN", "gs08": "004010", "st01": ["810"]},
    ],
}

SECOND_PROFILE = {
    "name": "retail",
    "isa05": "01",
    "isa06": "RETAILER",
    "isa07": "01",
    "isa08": "HUB",
    "gs02": "RETAILAPP",
    "gs03": "HUBAPP",
    "groups": [{"gs01": "SH", "gs08": "005010", "st01": ["856"]}],
}


def load_one(**overrides):
    """Load PROFILE (with optional field overrides) and return it."""
    return load_profiles(json.dumps([dict(PROFILE, **overrides)]))["acme"]


def isa(
    sender_qual: str = "ZZ",
    sender: str = "SENDER",
    receiver_qual: str = "ZZ",
    receiver: str = "PARTNER",
    control: str = "000000001",
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


def build_interchange(groups, **isa_kwargs) -> bytes:
    """Build a valid interchange.

    ``groups`` is a list of ``(gs_overrides, [st01, ...])`` pairs; GS
    fields default to the values PROFILE expects.
    """
    segs = [isa(**isa_kwargs)]
    for g_index, (gs, st01s) in enumerate(groups):
        segs.append(
            "GS*{}*{}*{}*20240101*1200*{}*X*{}~".format(
                gs.get("gs01", "PO"),
                gs.get("gs02", "SENDERAPP"),
                gs.get("gs03", "PARTNERAPP"),
                g_index + 1,
                gs.get("gs08", "005010"),
            )
        )
        for t_index, st01 in enumerate(st01s):
            ctrl = f"{g_index + 1}{t_index + 1:03d}"
            segs.append(f"ST*{st01}*{ctrl}~SE*2*{ctrl}~")
        segs.append(f"GE*{len(st01s)}*{g_index + 1}~")
    segs.append(f"IEA*{len(groups)}*000000001~")
    return "".join(segs).encode("ascii")


def envelope_of(raw: bytes):
    return audit(raw).envelope


class LoaderTests(unittest.TestCase):
    def test_unset_and_blank_disable_registry(self):
        self.assertEqual(load_profiles(None), {})
        self.assertEqual(load_profiles(""), {})
        self.assertEqual(load_profiles("   \n"), {})

    def test_valid_registry(self):
        registry = load_profiles(json.dumps([PROFILE, SECOND_PROFILE]))
        self.assertEqual(set(registry), {"acme", "retail"})
        profile = registry["acme"]
        self.assertEqual(profile.isa06, "SENDER")
        self.assertEqual(profile.gs03, "PARTNERAPP")
        self.assertEqual(len(profile.groups), 2)
        self.assertEqual(profile.groups[0].st01, frozenset({"850", "855"}))

    def test_max_profiles_accepted(self):
        cfg = [dict(PROFILE, name=f"p{i}") for i in range(MAX_PROFILES)]
        self.assertEqual(len(load_profiles(json.dumps(cfg))), MAX_PROFILES)

    def test_invalid_json(self):
        with self.assertRaises(ProfileConfigError):
            load_profiles("{not json")

    def test_top_level_not_array(self):
        with self.assertRaises(ProfileConfigError):
            load_profiles(json.dumps({"acme": PROFILE}))

    def test_profile_count_bounds(self):
        with self.assertRaises(ProfileConfigError):
            load_profiles("[]")
        too_many = [dict(PROFILE, name=f"p{i}") for i in range(MAX_PROFILES + 1)]
        with self.assertRaises(ProfileConfigError):
            load_profiles(json.dumps(too_many))

    def test_duplicate_names(self):
        with self.assertRaises(ProfileConfigError):
            load_profiles(json.dumps([PROFILE, PROFILE]))

    def test_profile_not_object(self):
        with self.assertRaises(ProfileConfigError):
            load_profiles(json.dumps(["acme"]))

    def test_missing_required_field(self):
        bad = dict(PROFILE)
        del bad["isa06"]
        with self.assertRaises(ProfileConfigError):
            load_profiles(json.dumps([bad]))
        with self.assertRaises(ProfileConfigError):
            load_one(isa06="")
        with self.assertRaises(ProfileConfigError):
            load_one(gs02=None)

    def test_isa_field_width_bounds(self):
        with self.assertRaises(ProfileConfigError):
            load_one(isa05="ZZZ")
        with self.assertRaises(ProfileConfigError):
            load_one(isa06="X" * 16)
        with self.assertRaises(ProfileConfigError):
            load_one(isa08="X" * 16)

    def test_non_ascii_value(self):
        with self.assertRaises(ProfileConfigError):
            load_one(isa06="SENDÉR")

    def test_groups_count_bounds(self):
        with self.assertRaises(ProfileConfigError):
            load_one(groups=[])
        too_many = [
            {"gs01": f"G{i}", "gs08": "005010", "st01": ["850"]}
            for i in range(MAX_GROUP_CONTRACTS + 1)
        ]
        with self.assertRaises(ProfileConfigError):
            load_one(groups=too_many)

    def test_contract_not_object(self):
        with self.assertRaises(ProfileConfigError):
            load_one(groups=["PO"])

    def test_st01_must_be_non_empty(self):
        with self.assertRaises(ProfileConfigError):
            load_one(groups=[{"gs01": "PO", "gs08": "005010", "st01": []}])
        with self.assertRaises(ProfileConfigError):
            load_one(groups=[{"gs01": "PO", "gs08": "005010"}])

    def test_st01_entries_must_be_strings(self):
        with self.assertRaises(ProfileConfigError):
            load_one(groups=[{"gs01": "PO", "gs08": "005010", "st01": [850]}])

    def test_duplicate_contract_pair(self):
        with self.assertRaises(ProfileConfigError):
            load_one(
                groups=[
                    {"gs01": "PO", "gs08": "005010", "st01": ["850"]},
                    {"gs01": "PO", "gs08": "005010", "st01": ["855"]},
                ]
            )

    def test_same_gs01_different_gs08_ok(self):
        profile = load_one(
            groups=[
                {"gs01": "PO", "gs08": "005010", "st01": ["850"]},
                {"gs01": "PO", "gs08": "004010", "st01": ["850"]},
            ]
        )
        self.assertEqual(len(profile.groups), 2)


class MatcherTests(unittest.TestCase):
    def setUp(self):
        self.profile = load_one()

    def mismatch(self, raw: bytes) -> ProfileMismatch:
        with self.assertRaises(ProfileMismatch) as ctx:
            check_envelope_against_profile(envelope_of(raw), self.profile)
        return ctx.exception

    def test_full_match(self):
        raw = build_interchange([({}, ["850", "855"])])
        check_envelope_against_profile(envelope_of(raw), self.profile)

    def test_second_contract_match(self):
        raw = build_interchange([({"gs01": "IN", "gs08": "004010"}, ["810"])])
        check_envelope_against_profile(envelope_of(raw), self.profile)

    def test_isa_right_padding_stripped(self):
        # ISA06 on the wire is the 15-char padded field; the configured
        # value carries no padding and must still match.
        raw = build_interchange([({}, ["850"])], sender="SENDER")
        check_envelope_against_profile(envelope_of(raw), self.profile)

    def test_isa_left_padding_not_stripped(self):
        raw = build_interchange([({}, ["850"])], sender=" SENDER")
        exc = self.mismatch(raw)
        self.assertEqual(exc.scope, "interchange")
        self.assertEqual(exc.segment, 1)

    def test_isa_case_sensitive(self):
        raw = build_interchange([({}, ["850"])], sender="sender")
        exc = self.mismatch(raw)
        self.assertEqual(exc.scope, "interchange")
        self.assertEqual(exc.segment, 1)

    def test_interchange_scope_and_segment(self):
        raw = build_interchange([({}, ["850"])], receiver="WRONG")
        exc = self.mismatch(raw)
        self.assertEqual(exc.scope, "interchange")
        self.assertEqual(exc.segment, 1)

    def test_group_gs02_mismatch(self):
        raw = build_interchange([({"gs02": "WRONG"}, ["850"])])
        exc = self.mismatch(raw)
        self.assertEqual(exc.scope, "group")
        self.assertEqual(exc.segment, 2)

    def test_group_gs03_mismatch(self):
        raw = build_interchange([({"gs03": "WRONG"}, ["850"])])
        exc = self.mismatch(raw)
        self.assertEqual(exc.scope, "group")
        self.assertEqual(exc.segment, 2)

    def test_uncontracted_gs01(self):
        raw = build_interchange([({"gs01": "XX"}, ["850"])])
        exc = self.mismatch(raw)
        self.assertEqual(exc.scope, "group")
        self.assertEqual(exc.segment, 2)

    def test_uncontracted_gs08(self):
        # GS01 alone matches a contract, but the GS01/GS08 pair does not.
        raw = build_interchange([({"gs08": "004010"}, ["850"])])
        exc = self.mismatch(raw)
        self.assertEqual(exc.scope, "group")
        self.assertEqual(exc.segment, 2)

    def test_gs01_case_sensitive(self):
        raw = build_interchange([({"gs01": "po"}, ["850"])])
        exc = self.mismatch(raw)
        self.assertEqual(exc.scope, "group")
        self.assertEqual(exc.segment, 2)

    def test_transaction_not_allowed(self):
        raw = build_interchange([({}, ["856"])])
        exc = self.mismatch(raw)
        self.assertEqual(exc.scope, "transaction")
        self.assertEqual(exc.segment, 3)

    def test_first_violation_in_message_order(self):
        # Group 1 carries a bad transaction (segment 5); group 2 is an
        # uncontracted GS (segment 7).  The earlier violation must win.
        raw = build_interchange([({}, ["850", "999"]), ({"gs01": "XX"}, ["850"])])
        exc = self.mismatch(raw)
        self.assertEqual(exc.scope, "transaction")
        self.assertEqual(exc.segment, 5)

    def test_later_group_violation_segment(self):
        raw = build_interchange([({}, ["850"]), ({"gs01": "XX"}, ["850"])])
        exc = self.mismatch(raw)
        self.assertEqual(exc.scope, "group")
        self.assertEqual(exc.segment, 6)


class ServerConfigTests(unittest.TestCase):
    def test_main_exits_non_zero_on_invalid_profiles(self):
        with mock.patch.dict(
            os.environ, {"X12_PARTNER_PROFILES": "{not json"}
        ):
            with self.assertRaises(SystemExit) as ctx:
                server.main()
        self.assertNotEqual(ctx.exception.code, 0)

    def test_create_server_defaults_to_empty_registry(self):
        srv = server.create_server("127.0.0.1", 0)
        try:
            self.assertEqual(srv.profiles, {})
        finally:
            srv.server_close()


class HttpProfileTests(unittest.TestCase):
    """End-to-end header behaviour against a real server instance."""

    @classmethod
    def setUpClass(cls):
        profiles = load_profiles(json.dumps([PROFILE]))
        cls.server = server.create_server("127.0.0.1", 0, profiles)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def post(self, body: bytes, profile: str | None = None):
        headers = {"Content-Type": "application/octet-stream"}
        if profile is not None:
            headers["X-Partner-Profile"] = profile
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/x12/audit",
            data=body,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_no_header_behaviour_unchanged(self):
        raw = build_interchange([({}, ["850"])], sender="ANYONE")
        status, payload = self.post(raw)
        self.assertEqual(status, 200)
        self.assertEqual(payload["transaction_count"], 1)

    def test_matching_profile(self):
        status, payload = self.post(
            build_interchange([({}, ["850"])]), profile="acme"
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["group_count"], 1)

    def test_unknown_profile(self):
        status, payload = self.post(
            build_interchange([({}, ["850"])]), profile="nobody"
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "PROFILE_NOT_FOUND")

    def test_mismatch_response_shape(self):
        raw = build_interchange([({}, ["999"])])
        status, payload = self.post(raw, profile="acme")
        self.assertEqual(status, 422)
        error = payload["error"]
        self.assertEqual(error["code"], "PROFILE_MISMATCH")
        self.assertEqual(error["scope"], "transaction")
        self.assertEqual(error["segment"], 3)
        self.assertTrue(error["message"])

    def test_envelope_error_outranks_profile_check(self):
        raw = (
            isa()
            + "GS*PO*SENDERAPP*PARTNERAPP*20240101*1200*1*X*005010~"
            + "ST*850*100~BEG*00~SE*2*100~"  # actual span is 3
            + "GE*1*1~IEA*1*000000001~"
        ).encode("ascii")
        status, payload = self.post(raw, profile="acme")
        self.assertEqual(status, 422)
        self.assertEqual(payload["error"]["code"], "SEGMENT_COUNT_MISMATCH")
        self.assertEqual(payload["error"]["segment"], 5)


if __name__ == "__main__":
    unittest.main()
