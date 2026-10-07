"""Unit tests for partner profile configuration and identity matching."""

from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request

from app.audit import audit_with_view
from app.profiles import (
    ENV_VAR,
    ProfileConfigError,
    ProfileMismatch,
    load_profiles_from_env,
    match,
    parse_profiles_json,
)
from app.server import create_server

ELEMENT = "*"
COMPONENT = ":"
TERMINATOR = "~"


def isa(
    control: str = "000000001",
    *,
    sender_qualifier: str = "ZZ",
    sender_id: str = "SENDER",
    receiver_qualifier: str = "ZZ",
    receiver_id: str = "PARTNER",
) -> str:
    fields = [
        "00",
        " " * 10,
        "00",
        " " * 10,
        sender_qualifier.ljust(2),
        sender_id.ljust(15),
        receiver_qualifier.ljust(2),
        receiver_id.ljust(15),
        " " * 6,
        " " * 4,
        "U",
        "00501",
        control.rjust(9),
        "0",
        "P",
        COMPONENT,
    ]
    return "ISA" + ELEMENT + ELEMENT.join(fields) + TERMINATOR


def build_bytes(
    *,
    groups: list[tuple[str, str, str, str, list[str]]] | None = None,
    sender_qualifier: str = "ZZ",
    sender_id: str = "SENDER",
    receiver_qualifier: str = "ZZ",
    receiver_id: str = "PARTNER",
) -> bytes:
    """Build a valid raw interchange.

    Each group is ``(GS01, GS02, GS03, GS08, [ST01, ...])``.
    """
    if groups is None:
        groups = [("PO", "SENDER", "PARTNER", "005010", ["850"])]
    segs = [
        isa(
            sender_qualifier=sender_qualifier,
            sender_id=sender_id,
            receiver_qualifier=receiver_qualifier,
            receiver_id=receiver_id,
        )
    ]
    for g_index, (gs01, gs02, gs03, gs08, st01s) in enumerate(groups):
        g_ctrl = str(g_index + 1)
        segs.append(
            f"GS*{gs01}*{gs02}*{gs03}*20240101*1200*{g_ctrl}*X*{gs08}"
            + TERMINATOR
        )
        for t_index, st01 in enumerate(st01s):
            t_ctrl = f"{g_index + 1}{t_index + 1:03d}"
            segs.append(f"ST*{st01}*{t_ctrl}" + TERMINATOR)
            segs.append(f"SE*2*{t_ctrl}" + TERMINATOR)
        segs.append(f"GE*{len(st01s)}*{g_ctrl}" + TERMINATOR)
    segs.append(f"IEA*{len(groups)}*000000001" + TERMINATOR)
    return "".join(segs).encode("ascii")


def build_view(**kwargs):
    return audit_with_view(build_bytes(**kwargs))[1]


def profile(
    *,
    name: str = "acme",
    isa05: str = "ZZ",
    isa06: str = "SENDER",
    isa07: str = "ZZ",
    isa08: str = "PARTNER",
    gs02: str = "SENDER",
    gs03: str = "PARTNER",
    groups: list[dict] | None = None,
) -> dict:
    if groups is None:
        groups = [{"GS01": "PO", "GS08": "005010", "ST01": ["850"]}]
    return {
        "name": name,
        "ISA05": isa05,
        "ISA06": isa06,
        "ISA07": isa07,
        "ISA08": isa08,
        "GS02": gs02,
        "GS03": gs03,
        "groups": groups,
    }


def parsed(single: dict | None = None) -> dict:
    return parse_profiles_json(json.dumps([single if single is not None else profile()]))


class ConfigValidationTests(unittest.TestCase):
    def test_valid_single_profile(self):
        profiles = parsed()
        self.assertEqual(set(profiles), {"acme"})
        contract = profiles["acme"].group_contracts[0]
        self.assertEqual(contract.functional_identifier, "PO")
        self.assertEqual(contract.version, "005010")
        self.assertEqual(set(contract.transaction_set_ids), {"850"})

    def test_valid_multiple_contracts_and_st_ids(self):
        data = profile(
            groups=[
                {"GS01": "PO", "GS08": "005010", "ST01": ["850", "855"]},
                {"GS01": "FA", "GS08": "005010", "ST01": ["997", "999"]},
            ]
        )
        result = parsed(data)["acme"]
        self.assertEqual(len(result.group_contracts), 2)

    def test_sixteen_profiles_allowed(self):
        entries = [profile(name=f"p{i:02d}") for i in range(16)]
        self.assertEqual(len(parse_profiles_json(json.dumps(entries))), 16)

    def test_too_many_profiles(self):
        entries = [profile(name=f"p{i:02d}") for i in range(17)]
        with self.assertRaises(ProfileConfigError):
            parse_profiles_json(json.dumps(entries))

    def test_empty_array(self):
        with self.assertRaises(ProfileConfigError):
            parse_profiles_json("[]")

    def test_not_json(self):
        with self.assertRaises(ProfileConfigError):
            parse_profiles_json("{not json")

    def test_not_array(self):
        with self.assertRaises(ProfileConfigError):
            parse_profiles_json(json.dumps(profile()))

    def test_missing_identity_field(self):
        for field in ("name", "ISA05", "ISA06", "ISA07", "ISA08", "GS02", "GS03"):
            with self.subTest(field=field):
                data = profile()
                del data[field]
                with self.assertRaises(ProfileConfigError):
                    parse_profiles_json(json.dumps([data]))

    def test_duplicate_profile_name(self):
        with self.assertRaises(ProfileConfigError):
            parse_profiles_json(json.dumps([profile(), profile()]))

    def test_empty_string_field(self):
        data = profile(isa06="")
        with self.assertRaises(ProfileConfigError):
            parsed(data)

    def test_non_string_field(self):
        data = profile(isa06=123)
        with self.assertRaises(ProfileConfigError):
            parsed(data)

    def test_non_ascii_value_rejected(self):
        data = profile(isa06="SÉNDER")
        with self.assertRaises(ProfileConfigError):
            parsed(data)
        data = profile(
            groups=[{"GS01": "PO", "GS08": "005010", "ST01": ["850", "85Ä"]}]
        )
        with self.assertRaises(ProfileConfigError):
            parsed(data)

    def test_groups_must_be_nonempty_array(self):
        with self.assertRaises(ProfileConfigError):
            parsed(profile(groups=[]))
        with self.assertRaises(ProfileConfigError):
            parsed(profile(groups="nope"))

    def test_too_many_group_contracts(self):
        groups = [
            {"GS01": "PO", "GS08": f"V{i}", "ST01": ["850"]}
            for i in range(9)
        ]
        with self.assertRaises(ProfileConfigError):
            parsed(profile(groups=groups))

    def test_duplicate_gs01_gs08_pair(self):
        groups = [
            {"GS01": "PO", "GS08": "005010", "ST01": ["850"]},
            {"GS01": "PO", "GS08": "005010", "ST01": ["855"]},
        ]
        with self.assertRaises(ProfileConfigError):
            parsed(profile(groups=groups))

    def test_st01_limits(self):
        with self.assertRaises(ProfileConfigError):
            parsed(profile(groups=[{"GS01": "PO", "GS08": "V", "ST01": []}]))
        with self.assertRaises(ProfileConfigError):
            parsed(
                profile(
                    groups=[
                        {"GS01": "PO", "GS08": "V", "ST01": ["1"] * 9}
                    ]
                )
            )
        with self.assertRaises(ProfileConfigError):
            parsed(
                profile(
                    groups=[
                        {"GS01": "PO", "GS08": "V", "ST01": ["850", "850"]}
                    ]
                )
            )
        with self.assertRaises(ProfileConfigError):
            parsed(
                profile(
                    groups=[{"GS01": "PO", "GS08": "V", "ST01": [""]}]
                )
            )

    def test_unknown_fields_rejected(self):
        data = profile()
        data["extra"] = 1
        with self.assertRaises(ProfileConfigError):
            parsed(data)
        data = profile()
        data["groups"][0]["bogus"] = 1
        with self.assertRaises(ProfileConfigError):
            parsed(data)

    def test_value_too_long_for_isa_field(self):
        with self.assertRaises(ProfileConfigError):
            parsed(profile(isa06="A" * 16))
        with self.assertRaises(ProfileConfigError):
            parsed(profile(isa05="ZZZ"))

    def test_env_absent_or_blank_disables(self):
        self.assertIsNone(load_profiles_from_env({}))
        self.assertIsNone(load_profiles_from_env({ENV_VAR: ""}))
        self.assertIsNone(load_profiles_from_env({ENV_VAR: "   "}))

    def test_env_invalid_raises(self):
        with self.assertRaises(ProfileConfigError):
            load_profiles_from_env({ENV_VAR: "nope"})

    def test_env_valid_loads(self):
        text = json.dumps([profile()])
        result = load_profiles_from_env({ENV_VAR: text})
        self.assertIsNotNone(result)
        self.assertIn("acme", result)


class MatchTests(unittest.TestCase):
    def test_matching_envelope_passes(self):
        match(parsed()["acme"], build_view())

    def test_multiple_groups_and_transactions_pass(self):
        p = parsed(
            profile(
                groups=[
                    {"GS01": "PO", "GS08": "005010", "ST01": ["850", "855"]},
                    {"GS01": "FA", "GS08": "005010", "ST01": ["997"]},
                ]
            )
        )["acme"]
        view = build_view(
            groups=[
                ("PO", "SENDER", "PARTNER", "005010", ["850", "855"]),
                ("FA", "SENDER", "PARTNER", "005010", ["997"]),
            ]
        )
        match(p, view)

    def test_sender_mismatch_reports_interchange_segment_1(self):
        view = build_view(sender_id="INTRUDER")
        with self.assertRaises(ProfileMismatch) as ctx:
            match(parsed()["acme"], view)
        self.assertEqual(ctx.exception.scope, "interchange")
        self.assertEqual(ctx.exception.segment, 1)

    def test_receiver_mismatch_reports_interchange_segment_1(self):
        view = build_view(receiver_id="SOMEWHERE")
        with self.assertRaises(ProfileMismatch) as ctx:
            match(parsed()["acme"], view)
        self.assertEqual(ctx.exception.scope, "interchange")
        self.assertEqual(ctx.exception.segment, 1)

    def test_sender_qualifier_mismatch(self):
        view = build_view(sender_qualifier="01")
        with self.assertRaises(ProfileMismatch) as ctx:
            match(parsed()["acme"], view)
        self.assertEqual(ctx.exception.scope, "interchange")

    def test_gs02_mismatch(self):
        view = build_view(groups=[("PO", "OTHER", "PARTNER", "005010", ["850"])])
        with self.assertRaises(ProfileMismatch) as ctx:
            match(parsed()["acme"], view)
        self.assertEqual(ctx.exception.scope, "group")
        # ISA=1, GS=2
        self.assertEqual(ctx.exception.segment, 2)

    def test_gs03_mismatch(self):
        view = build_view(groups=[("PO", "SENDER", "ELSE", "005010", ["850"])])
        with self.assertRaises(ProfileMismatch) as ctx:
            match(parsed()["acme"], view)
        self.assertEqual(ctx.exception.scope, "group")
        self.assertEqual(ctx.exception.segment, 2)

    def test_gs01_not_contracted(self):
        view = build_view(groups=[("IN", "SENDER", "PARTNER", "005010", ["850"])])
        with self.assertRaises(ProfileMismatch) as ctx:
            match(parsed()["acme"], view)
        self.assertEqual(ctx.exception.scope, "group")
        self.assertEqual(ctx.exception.segment, 2)

    def test_gs08_not_contracted(self):
        view = build_view(groups=[("PO", "SENDER", "PARTNER", "004010", ["850"])])
        with self.assertRaises(ProfileMismatch) as ctx:
            match(parsed()["acme"], view)
        self.assertEqual(ctx.exception.scope, "group")
        self.assertEqual(ctx.exception.segment, 2)

    def test_st01_not_allowed(self):
        view = build_view(groups=[("PO", "SENDER", "PARTNER", "005010", ["997"])])
        with self.assertRaises(ProfileMismatch) as ctx:
            match(parsed()["acme"], view)
        self.assertEqual(ctx.exception.scope, "transaction")
        # ISA=1, GS=2, ST=3
        self.assertEqual(ctx.exception.segment, 3)

    def test_st01_allowed_set_distinguishes_contracts(self):
        # 850 is allowed under PO/005010 but not under FA/005010.
        p = parsed(
            profile(
                groups=[
                    {"GS01": "PO", "GS08": "005010", "ST01": ["850"]},
                    {"GS01": "FA", "GS08": "005010", "ST01": ["997"]},
                ]
            )
        )["acme"]
        view = build_view(groups=[("FA", "SENDER", "PARTNER", "005010", ["850"])])
        with self.assertRaises(ProfileMismatch) as ctx:
            match(p, view)
        self.assertEqual(ctx.exception.scope, "transaction")

    def test_first_violating_group_reported_in_order(self):
        view = build_view(
            groups=[
                ("PO", "SENDER", "PARTNER", "005010", ["850"]),
                ("PO", "SENDER", "PARTNER", "004010", ["850"]),
            ]
        )
        with self.assertRaises(ProfileMismatch) as ctx:
            match(parsed()["acme"], view)
        self.assertEqual(ctx.exception.scope, "group")
        # First group: ISA,GS,ST,SE,GE = 1..5; second GS is segment 6.
        self.assertEqual(ctx.exception.segment, 6)

    def test_first_violating_transaction_reported_in_order(self):
        view = build_view(
            groups=[("PO", "SENDER", "PARTNER", "005010", ["850", "997"])]
        )
        with self.assertRaises(ProfileMismatch) as ctx:
            match(parsed()["acme"], view)
        self.assertEqual(ctx.exception.scope, "transaction")
        # ISA=1, GS=2, ST=3, SE=4, ST=5 -> the 997 ST is segment 5.
        self.assertEqual(ctx.exception.segment, 5)

    def test_isa_trailing_space_padding_ignored(self):
        # The envelope carries the 15-char padded ISA06; the profile
        # declares the unpadded logical id and still matches.
        match(parsed(profile(isa06="SEN"))["acme"], build_view(sender_id="SEN"))

    def test_isa_leading_spaces_are_significant(self):
        view = build_view(sender_id=" SEN")
        with self.assertRaises(ProfileMismatch):
            match(parsed(profile(isa06="SEN"))["acme"], view)
        match(
            parsed(profile(isa06=" SEN"))["acme"],
            view,
        )

    def test_identifiers_are_case_sensitive(self):
        # Lowercase ISA06 must not match the uppercase profile value.
        view = build_view(sender_id="sender")
        with self.assertRaises(ProfileMismatch):
            match(parsed()["acme"], view)

        # ST01 codes are likewise case-sensitive.
        p = parsed(
            profile(
                groups=[{"GS01": "PO", "GS08": "005010", "ST01": ["ABC"]}]
            )
        )["acme"]
        view = build_view(
            groups=[("PO", "SENDER", "PARTNER", "005010", ["abc"])]
        )
        with self.assertRaises(ProfileMismatch) as ctx:
            match(p, view)
        self.assertEqual(ctx.exception.scope, "transaction")

    def test_gs01_gs08_case_sensitive(self):
        p = parsed(
            profile(groups=[{"GS01": "po", "GS08": "005010", "ST01": ["850"]}])
        )["acme"]
        view = build_view(groups=[("PO", "SENDER", "PARTNER", "005010", ["850"])])
        with self.assertRaises(ProfileMismatch) as ctx:
            match(p, view)
        self.assertEqual(ctx.exception.scope, "group")


class HttpProfileTests(unittest.TestCase):
    """End-to-end checks of the X-Partner-Profile header handling."""

    @classmethod
    def setUpClass(cls):
        profiles = parse_profiles_json(
            json.dumps(
                [
                    profile(),
                    profile(
                        name="beta",
                        isa06="BETA",
                        gs02="BETA",
                        groups=[
                            {
                                "GS01": "FA",
                                "GS08": "005010",
                                "ST01": ["997", "999"],
                            }
                        ],
                    ),
                ]
            )
        )
        cls._server = create_server("127.0.0.1", 0, profiles)
        cls._thread = threading.Thread(
            target=cls._server.serve_forever, daemon=True
        )
        cls._thread.start()
        host, port = cls._server.server_address[:2]
        cls.base_url = f"http://{host}:{port}"

    @classmethod
    def tearDownClass(cls):
        cls._server.shutdown()
        cls._thread.join(timeout=5)
        cls._server.server_close()

    def _post(self, raw: bytes, partner: str | None = None, **headers):
        hdrs = {"Content-Type": "application/octet-stream"}
        if partner is not None:
            hdrs["X-Partner-Profile"] = partner
        hdrs.update(headers)
        req = urllib.request.Request(
            f"{self.base_url}/api/x12/audit",
            data=raw,
            headers=hdrs,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_compat_no_header_success(self):
        status, payload = self._post(build_bytes())
        self.assertEqual(status, 200)
        self.assertEqual(payload["group_count"], 1)
        self.assertEqual(payload["transaction_count"], 1)
        self.assertNotIn("error", payload)

    def test_compat_no_header_envelope_error_unchanged(self):
        # Truncated message with no profile header: original behavior.
        status, payload = self._post(b"")
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "EMPTY_MESSAGE")

    def test_header_match_success(self):
        status, payload = self._post(build_bytes(), partner="acme")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["transaction_count"], 1)

    def test_header_match_multiple_transactions(self):
        body = build_bytes(
            sender_id="BETA",
            groups=[("FA", "BETA", "PARTNER", "005010", ["997", "999"])],
        )
        status, payload = self._post(body, partner="beta")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["transaction_count"], 2)

    def test_unknown_profile_is_400(self):
        status, payload = self._post(build_bytes(), partner="ghost")
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "PROFILE_NOT_FOUND")
        self.assertTrue(payload["error"]["message"])
        # Identity/selection errors carry no segment/scope fields.
        self.assertNotIn("segment", payload["error"])
        self.assertNotIn("scope", payload["error"])

    def test_unknown_profile_takes_priority_over_envelope_errors(self):
        # Even an empty body returns PROFILE_NOT_FOUND when the named
        # profile does not exist.
        status, payload = self._post(b"", partner="ghost")
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "PROFILE_NOT_FOUND")

    def test_transport_gates_keep_precedence_over_profile_lookup(self):
        # Wrong Content-Type still yields 415 even with an unknown profile.
        req = urllib.request.Request(
            f"{self.base_url}/api/x12/audit",
            data=b"x",
            headers={
                "Content-Type": "text/plain",
                "X-Partner-Profile": "ghost",
            },
            method="POST",
        )
        try:
            urllib.request.urlopen(req)
            self.fail("expected 415")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 415)
            payload = json.loads(exc.read())
            self.assertEqual(
                payload["error"]["code"], "UNSUPPORTED_MEDIA_TYPE"
            )

    def test_unknown_profile_header_without_config(self):
        server = create_server("127.0.0.1", 0, None)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = server.server_address[:2]
            req = urllib.request.Request(
                f"http://{host}:{port}/api/x12/audit",
                data=build_bytes(),
                headers={
                    "Content-Type": "application/octet-stream",
                    "X-Partner-Profile": "acme",
                },
                method="POST",
            )
            try:
                urllib.request.urlopen(req)
                self.fail("expected 400")
            except urllib.error.HTTPError as exc:
                self.assertEqual(exc.code, 400)
                payload = json.loads(exc.read())
                self.assertEqual(
                    payload["error"]["code"], "PROFILE_NOT_FOUND"
                )
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()

    def test_envelope_error_takes_priority_over_mismatch(self):
        # Wrong ISA sender AND a truncated final segment: the envelope
        # audit must surface first.
        truncated = build_bytes(sender_id="INTRUDER")[:-1]
        status, payload = self._post(truncated, partner="acme")
        self.assertEqual(status, 422)
        self.assertEqual(payload["error"]["code"], "MISSING_TERMINATOR")
        self.assertEqual(payload["error"]["segment"], 6)

    def test_sender_mismatch_is_422(self):
        status, payload = self._post(
            build_bytes(sender_id="INTRUDER"), partner="acme"
        )
        self.assertEqual(status, 422)
        error = payload["error"]
        self.assertEqual(error["code"], "PROFILE_MISMATCH")
        self.assertEqual(error["scope"], "interchange")
        self.assertEqual(error["segment"], 1)

    def test_gs03_mismatch_is_422(self):
        body = build_bytes(
            groups=[("PO", "SENDER", "ELSE", "005010", ["850"])]
        )
        status, payload = self._post(body, partner="acme")
        self.assertEqual(status, 422)
        error = payload["error"]
        self.assertEqual(error["code"], "PROFILE_MISMATCH")
        self.assertEqual(error["scope"], "group")
        self.assertEqual(error["segment"], 2)

    def test_gs01_gs08_uncontracted_is_422(self):
        body = build_bytes(
            groups=[("FA", "SENDER", "PARTNER", "005010", ["997"])]
        )
        status, payload = self._post(body, partner="acme")
        self.assertEqual(status, 422)
        error = payload["error"]
        self.assertEqual(error["code"], "PROFILE_MISMATCH")
        self.assertEqual(error["scope"], "group")
        self.assertEqual(error["segment"], 2)

    def test_st01_forbidden_is_422_with_transaction_scope(self):
        body = build_bytes(
            groups=[("PO", "SENDER", "PARTNER", "005010", ["997"])]
        )
        status, payload = self._post(body, partner="acme")
        self.assertEqual(status, 422)
        error = payload["error"]
        self.assertEqual(error["code"], "PROFILE_MISMATCH")
        self.assertEqual(error["scope"], "transaction")
        self.assertEqual(error["segment"], 3)

    def test_second_transaction_mismatch_reports_its_st_segment(self):
        body = build_bytes(
            groups=[("PO", "SENDER", "PARTNER", "005010", ["850", "997"])]
        )
        status, payload = self._post(body, partner="acme")
        self.assertEqual(status, 422)
        error = payload["error"]
        self.assertEqual(error["code"], "PROFILE_MISMATCH")
        self.assertEqual(error["scope"], "transaction")
        # ISA=1, GS=2, ST=3, SE=4, ST(997)=5
        self.assertEqual(error["segment"], 5)

    def test_unknown_profile_drains_body_keeps_connection_alive(self):
        # The 400 response must consume the request body so a second
        # request on the same keep-alive connection still works.
        host, port = self._server.server_address[:2]
        conn = http.client.HTTPConnection(host, port)
        try:
            conn.request(
                "POST",
                "/api/x12/audit",
                body=build_bytes(),
                headers={
                    "Content-Type": "application/octet-stream",
                    "X-Partner-Profile": "ghost",
                },
            )
            response = conn.getresponse()
            self.assertEqual(response.status, 400)
            payload = json.loads(response.read())
            self.assertEqual(payload["error"]["code"], "PROFILE_NOT_FOUND")

            conn.request(
                "POST",
                "/api/x12/audit",
                body=build_bytes(),
                headers={"Content-Type": "application/octet-stream"},
            )
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            payload = json.loads(response.read())
            self.assertEqual(payload["transaction_count"], 1)
        finally:
            conn.close()


class StartupTests(unittest.TestCase):
    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def _free_port(self) -> int:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    def test_invalid_config_exits_nonzero(self):
        env = dict(os.environ, PORT="0", X12_PARTNER_PROFILES="not-json")
        completed = subprocess.run(
            [sys.executable, "-m", "app.server"],
            cwd=self.ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
        )
        self.assertNotEqual(completed.returncode, 0)
    def test_valid_config_serves_health(self):
        port = self._free_port()
        config = json.dumps(
            [
                profile(),
                profile(name="beta", gs02="BETA", gs03="PARTNER"),
            ]
        )
        env = dict(
            os.environ,
            PORT=str(port),
            HOST="127.0.0.1",
            X12_PARTNER_PROFILES=config,
        )
        proc = subprocess.Popen(
            [sys.executable, "-m", "app.server"],
            cwd=self.ROOT,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.time() + 20
            while time.time() < deadline:
                try:
                    with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/health", timeout=1
                    ) as response:
                        self.assertEqual(response.status, 200)
                    break
                except OSError:
                    if proc.poll() is not None:
                        self.fail("server exited early")
                    time.sleep(0.1)
            else:
                self.fail("server never became healthy")
        finally:
            proc.terminate()
            proc.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
