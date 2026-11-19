"""Ownership checks protect unrelated processes; no live process is terminated by these tests."""
import importlib.util
import os
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("studio_local_model", Path(__file__).resolve().parents[1] / "scripts" / "local_model.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@unittest.skipUnless(os.name == "nt", "Windows process identity API")
class ModelOwnershipTests(unittest.TestCase):
    def test_identity_and_exact_ownership(self):
        actual = module.identity(os.getpid())
        self.assertTrue(Path(actual["executable"]).is_absolute())
        state = {"owner": "voice-studio-local-model", "pid": os.getpid(), **actual}
        self.assertTrue(module.owned(state))
        self.assertFalse(module.owned({**state, "created_filetime": actual["created_filetime"] + 1}))
        self.assertFalse(module.owned({**state, "executable": actual["executable"] + ".other"}))

    def test_unowned_pid_is_never_stopped(self):
        state = {"owner": "some-other-app", "pid": os.getpid(), **module.identity(os.getpid())}
        self.assertEqual(module.stop(state)["status"], "not_owned_or_not_running")
        self.assertIsNotNone(module.identity(os.getpid()))
