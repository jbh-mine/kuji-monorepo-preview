import base64
import json
import unittest

from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from account_sync_config import validate_account_config


def config(**values):
    return {"version": 1, "provider": "supabase", "url": "https://kujitest.supabase.co", "publishableKey": "sb_publishable_public_fixture_only", **values}


def legacy(role):
    body = base64.urlsafe_b64encode(json.dumps({"role": role}).encode()).decode().rstrip("=")
    return "eyJhbGciOiJIUzI1NiJ9." + body + ".fixture"


class AccountSyncConfigTests(unittest.TestCase):
    def test_disconnected_pair_is_explicit_and_safe(self):
        self.assertFalse(validate_account_config(config(url="", publishableKey="")))
        self.assertTrue(validate_account_config(config()))
        self.assertTrue(validate_account_config(config(publishableKey=legacy("anon"))))

    def test_secret_and_service_role_keys_are_never_public(self):
        for key in ["sb_secret_not_for_browser", legacy("service_role"), legacy("authenticated"), "bad-key", "a.%%%%.x"]:
            with self.subTest(key_type=key.split("_")[0]):
                with self.assertRaises(ValueError):
                    validate_account_config(config(publishableKey=key))

    def test_redirects_queries_credentials_ports_and_foreign_hosts_are_rejected(self):
        for url in ["http://kujitest.supabase.co", "https://kujitest.supabase.co.evil.test", "https://user@kujitest.supabase.co", "https://kujitest.supabase.co:443", "https://kujitest.supabase.co/x", "https://kujitest.supabase.co?key=private", "https://kujitest.supabase.co#token", " https://kujitest.supabase.co", "https://kujitest.supabase.co\n"]:
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    validate_account_config(config(url=url))

    def test_extra_fields_partial_config_and_oversized_values_fail_closed(self):
        for value in [None, [], config(secret="do not publish"), config(url=""), config(publishableKey=""), config(version=2), config(version=True), config(provider="unknown"), config(publishableKey="x" * 4097)]:
            with self.assertRaises(ValueError):
                validate_account_config(value)
