"""Run with: python3 -m unittest discover -s ios/Tests -p 'test_*.py'."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location("configure_ios", Path(__file__).resolve().parents[2] / "scripts/configure_ios.py")
CONFIG = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CONFIG)


class ConnectionConfigTests(unittest.TestCase):
    def test_phone_name_is_independent_of_server_bind_address(self):
        rendered = CONFIG.render({"CLAIM_HOST":"0.0.0.0", "CLAIM_PHONE_HOST":"paired-mac.local",
                                  "CLAIM_PORT":"4180", "CLAIM_PAIR_TOKEN":"test-token"})
        self.assertIn("http:/$()/paired-mac.local:4180", rendered)
        self.assertNotIn("0.0.0.0", rendered)

    def test_only_connection_settings_enter_build(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / ".env"
            source.write_text('CLAIM_HOST=0.0.0.0\nCLAIM_PHONE_HOST=paired-mac.local\nCLAIM_PORT=4180\nCLAIM_PAIR_TOKEN="paired-test-token"\nOPENAI_API_KEY=must-stay-on-mac\nGEMINI_API_KEY=also-stays-on-mac\nANTHROPIC_API_KEY=claude-stays-on-mac\n')
            values = CONFIG.connection_values(source)
        self.assertEqual(set(values), CONFIG.CONNECTION_KEYS)
        rendered = CONFIG.render(values)
        self.assertIn("http:/$()/paired-mac.local:4180", rendered)
        self.assertNotIn("must-stay-on-mac", rendered)
        self.assertNotIn("also-stays-on-mac", rendered)
        self.assertNotIn("claude-stays-on-mac", rendered)

    def test_legacy_host_still_works_without_phone_override(self):
        for override in ({}, {"CLAIM_PHONE_HOST":""}):
            rendered = CONFIG.render({"CLAIM_HOST":"192.168.1.10", "CLAIM_PORT":"4180",
                                      "CLAIM_PAIR_TOKEN":"test-token", **override})
            self.assertIn("http:/$()/192.168.1.10:4180", rendered)

    def test_rejects_build_setting_injection_and_wildcard(self):
        base = {"CLAIM_HOST":"mac.local", "CLAIM_PORT":"4180", "CLAIM_PAIR_TOKEN":"test-token"}
        for key, unsafe in [("CLAIM_HOST","0.0.0.0"), ("CLAIM_HOST","mac/$(OPENAI_API_KEY)"),
                            ("CLAIM_PHONE_HOST","0.0.0.0"), ("CLAIM_PHONE_HOST","::"),
                            ("CLAIM_PHONE_HOST","mac/$(OPENAI_API_KEY)"),
                            ("CLAIM_PORT","99999"), ("CLAIM_PAIR_TOKEN","$(OPENAI_API_KEY)")]:
            with self.subTest(key=key, unsafe=unsafe):
                with self.assertRaises(ValueError):
                    CONFIG.render({**base, key:unsafe})


if __name__ == "__main__":
    unittest.main()
