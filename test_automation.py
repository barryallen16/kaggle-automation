import unittest
import json
import uuid
import asyncio
from pathlib import Path
from app.config import DATA_DIR, NOTEBOOKS_DIR
from app.database import (
    init_db, save_account, get_all_accounts, create_run_record,
    get_all_runs, get_run_by_id, update_run_status
)
from app.services.workload_distributor import WorkloadDistributor
from app.services.account_manager import AccountManager

class TestKaggleAutomation(unittest.TestCase):
    def setUp(self):
        init_db()

    def test_database_and_account_crud(self):
        test_user = "test_user_unit"
        save_account("acc_123", test_user, "dummy_token_abc", {"gpu": {"used": 5, "limit": 30, "percent": 16.7}})
        
        accounts = get_all_accounts()
        usernames = [a["username"] for a in accounts]
        self.assertIn(test_user, usernames)

        # Test isolated config directory
        cfg_dir = AccountManager.get_account_config_dir(test_user)
        self.assertTrue(cfg_dir.exists())

    def test_run_crud(self):
        run_id = f"test_run_{uuid.uuid4().hex[:6]}"
        create_run_record({
            "id": run_id,
            "account_username": "test_user_unit",
            "kernel_slug": "unit-test-kernel",
            "kernel_ref": "test_user_unit/unit-test-kernel",
            "title": "Unit Test Kernel",
            "code_file": "main.py",
            "accelerator": "nvidia-tesla-t4-x2",
            "enable_internet": 1,
            "is_trial": 1,
            "timeout_seconds": 300,
            "status": "queued",
            "status_message": "Queued",
            "start_time": "2026-08-20T21:00:00",
            "kaggle_url": "https://www.kaggle.com/code/test_user_unit/unit-test-kernel",
            "workload_id": None,
            "shard_index": None,
            "total_shards": None,
            "log_file": "test.log"
        })

        run = get_run_by_id(run_id)
        self.assertIsNotNone(run)
        self.assertEqual(run["status"], "queued")
        self.assertEqual(run["accelerator"], "nvidia-tesla-t4-x2")

        update_run_status(run_id, "running")
        updated = get_run_by_id(run_id)
        self.assertEqual(updated["status"], "running")

    def test_workload_sharding_and_code_injection(self):
        total_items = 10000000
        num_accounts = 4
        chunk_size = total_items // num_accounts
        
        self.assertEqual(chunk_size, 2500000)

        # Test python script injection
        raw_py = "for i in range(START_INDEX, END_INDEX):\n    pass"
        injected_py = WorkloadDistributor.inject_shard_config_into_notebook(
            code_content=raw_py,
            filename="worker.py",
            shard_id=1,
            total_shards=4,
            start_index=2500000,
            end_index=5000000,
            total_items=10000000
        )
        self.assertIn("SHARD_ID = 1", injected_py)
        self.assertIn("START_INDEX = 2500000", injected_py)
        self.assertIn("END_INDEX = 5000000", injected_py)

        # Test .ipynb injection
        raw_nb = json.dumps({
            "cells": [
                {"cell_type": "code", "source": ["print('Hello')"]}
            ],
            "metadata": {},
            "nbformat": 4,
            "nbformat_minor": 2
        })
        injected_nb = WorkloadDistributor.inject_shard_config_into_notebook(
            code_content=raw_nb,
            filename="notebook.ipynb",
            shard_id=0,
            total_shards=4,
            start_index=0,
            end_index=2500000,
            total_items=10000000
        )
        parsed = json.loads(injected_nb)
        self.assertEqual(len(parsed["cells"]), 2)
        self.assertIn("SHARD_ID = 0", "".join(parsed["cells"][0]["source"]))

if __name__ == "__main__":
    unittest.main()
