"""Offline inventory lifecycle tests. All writes stay in a temporary database."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import database
import main


def interaction(user_id=123):
    response = SimpleNamespace(is_done=lambda: False, defer=AsyncMock(), send_message=AsyncMock())
    return SimpleNamespace(
        guild=object(),
        user=SimpleNamespace(id=user_id),
        response=response,
        followup=SimpleNamespace(send=AsyncMock()),
    )


class InventoryDeliveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        for name, value in (
            ("DB_FILE", str(Path(temp.name) / "vending.json")),
            ("BACKUP_FILE", str(Path(temp.name) / "vending.json.bak")),
            ("LEGACY_DB_FILE", str(Path(temp.name) / "legacy.json")),
        ):
            setting = patch.object(database, name, value)
            setting.start()
            self.addCleanup(setting.stop)

    def item(self, contents="a\nb", *, unlimited=False, price=100):
        return database.add_item("機械", "商品", price, contents, unlimited=unlimited)

    def test_pending_paypay_orders_do_not_reserve_or_reduce_stock(self):
        item = self.item()
        orders = [database.create_order(buyer, item["id"], 2) for buyer in (101, 102, 103)]
        self.assertTrue(all(order["reserved_contents"] == [] for order in orders))
        self.assertEqual(database.get_item(item["id"])["stock"], 2)
        self.assertEqual(database.get_item(item["id"])["contents"], ["a", "b"])
        self.assertEqual(database.begin_delivery(orders[0]["id"])["reserved_contents"], ["a", "b"])
        self.assertEqual(database.get_item(item["id"])["stock"], 2)
        with self.assertRaisesRegex(database.OrderError, "在庫が足りません"):
            database.create_order(104, item["id"], 1)
        self.assertTrue(database.complete_order(orders[0]["id"]))
        self.assertEqual(database.get_item(item["id"])["stock"], 0)

    def test_two_deliveries_have_no_overlapping_lines(self):
        item = self.item("a\nb\nc")
        first = database.create_order(101, item["id"], 2)
        second = database.create_order(102, item["id"], 1)
        a = database.begin_delivery(first["id"])
        b = database.begin_delivery(second["id"])
        self.assertEqual(a["reserved_contents"], ["a", "b"])
        self.assertEqual(b["reserved_contents"], ["c"])
        self.assertEqual(database.get_item(item["id"])["stock"], 3)
        self.assertTrue(database.complete_order(first["id"]))
        self.assertTrue(database.complete_order(second["id"]))
        self.assertEqual(database.get_item(item["id"])["contents"], [])

    def test_confirmation_insufficient_inventory_is_explicit_and_retryable(self):
        item = self.item("a")
        first = database.create_order(101, item["id"], 1)
        second = database.create_order(102, item["id"], 1)
        database.begin_delivery(first["id"])
        with self.assertRaisesRegex(database.OrderError, "在庫が足りません.*0個"):
            database.begin_delivery(second["id"])
        self.assertEqual(database.get_order(second["id"])["status"], "pending")
        self.assertEqual(database.get_order(second["id"])["reserved_contents"], [])
        self.assertTrue(database.release_order(first["id"]))
        self.assertEqual(database.begin_delivery(second["id"])["reserved_contents"], ["a"])

    def test_old_uncommitted_pending_reservations_are_ignored_and_reallocated(self):
        item = self.item("a\nb")
        old = database.create_order(101, item["id"], 1)
        newer = database.create_order(102, item["id"], 1)
        data = database.load_data()
        data["orders"][0]["reserved_contents"] = ["obsolete"]
        database._save(data)
        self.assertEqual(database.begin_delivery(newer["id"])["reserved_contents"], ["a"])
        self.assertEqual(database.begin_delivery(old["id"])["reserved_contents"], ["b"])
        self.assertTrue(database.complete_order(old["id"]))
        self.assertEqual(database.get_item(item["id"])["contents"], ["a"])

    def test_legacy_committed_order_keeps_previously_removed_contents(self):
        item = self.item("a\nb")
        old = database.create_order(101, item["id"], 1)
        data = database.load_data()
        data["orders"][0]["reserved_contents"] = ["a"]
        data["orders"][0].pop("inventory_committed")
        data["items"][0]["contents"] = ["b"]
        database._save(data)
        self.assertTrue(database.get_order(old["id"])["inventory_committed"])
        delivery = database.begin_delivery(old["id"])
        self.assertEqual(delivery["reserved_contents"], ["a"])
        self.assertTrue(database.complete_order(old["id"]))
        self.assertEqual(database.get_item(item["id"])["contents"], ["b"])

    def test_unlimited_template_assigned_at_confirmation(self):
        item = self.item("original", unlimited=True)
        order = database.create_order(101, item["id"], 3)
        self.assertEqual(order["reserved_contents"], [])
        database.update_item(item["id"], contents="updated")
        self.assertEqual(database.begin_delivery(order["id"])["reserved_contents"], ["updated"] * 3)
        self.assertTrue(database.complete_order(order["id"]))
        self.assertEqual(database.get_item(item["id"])["stock"], -1)
        self.assertEqual(database.get_item(item["id"])["sold_count"], 3)

    def test_admin_dm_failure_releases_without_consuming_stock(self):
        item = self.item("a")
        order = database.create_order(101, item["id"], 1)
        modal = main.DeliveryConfirmModal(order["id"])
        modal.confirmation._value = "確認"
        i = interaction(main.AUTHORIZED_OWNER_ID)
        with patch.object(main.bot, "fetch_user", new_callable=AsyncMock) as fetch:
            fetch.return_value.send = AsyncMock(side_effect=main.discord.Forbidden(
                SimpleNamespace(status=403, reason="Forbidden"), "DM blocked"
            ))
            asyncio.run(modal.on_submit(i))
        self.assertEqual(database.get_order(order["id"])["status"], "cancelled")
        self.assertEqual(database.get_item(item["id"])["contents"], ["a"])
        self.assertIn("DM送信に失敗", i.response.send_message.call_args.args[0])

    def test_admin_confirmation_reports_stock_error_without_sending_dm(self):
        item = self.item("a")
        first = database.create_order(101, item["id"], 1)
        second = database.create_order(102, item["id"], 1)
        database.begin_delivery(first["id"])
        modal = main.DeliveryConfirmModal(second["id"])
        modal.confirmation._value = "確認"
        i = interaction(main.AUTHORIZED_OWNER_ID)
        with patch.object(main.bot, "fetch_user", new_callable=AsyncMock) as fetch:
            asyncio.run(modal.on_submit(i))
            fetch.assert_not_awaited()
        self.assertEqual(database.get_order(second["id"])["status"], "pending")
        self.assertIn("在庫が足りません", i.response.send_message.call_args.args[0])

    def test_free_delivery_does_not_send_stale_order_when_begin_fails(self):
        item = self.item("a", price=0)
        modal = main.PurchaseModal(item)
        modal.quantity._value = "1"
        i = interaction()
        with (
            patch.object(database, "begin_delivery", side_effect=database.OrderError("在庫が足りません")),
            patch.object(main.bot, "fetch_user", new_callable=AsyncMock) as fetch,
        ):
            asyncio.run(modal.on_submit(i))
            fetch.assert_not_awaited()
        orders = database.load_data()["orders"]
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["status"], "cancelled")
        self.assertEqual(database.get_item(item["id"])["stock"], 1)
        self.assertIn("在庫が足りません", i.response.send_message.call_args.args[0])

    def test_free_dm_failure_releases_stock_and_success_consumes_it(self):
        item = self.item("a", price=0)
        failed = main.PurchaseModal(item)
        failed.quantity._value = "1"
        i = interaction()
        with patch.object(main.bot, "fetch_user", new_callable=AsyncMock) as fetch:
            fetch.return_value.send = AsyncMock(side_effect=main.discord.Forbidden(
                SimpleNamespace(status=403, reason="Forbidden"), "DM blocked"
            ))
            asyncio.run(failed.on_submit(i))
        self.assertEqual(database.get_item(item["id"])["stock"], 1)
        self.assertEqual(database.load_data()["orders"][0]["status"], "cancelled")

        successful = main.PurchaseModal(item)
        successful.quantity._value = "1"
        with (
            patch.object(main.bot, "fetch_user", new_callable=AsyncMock) as fetch,
            patch.object(main, "refresh_all", new_callable=AsyncMock),
            patch.object(main, "send_achievement_log", new_callable=AsyncMock),
        ):
            async def send_and_check(*args, **kwargs):
                self.assertEqual(database.get_item(item["id"])["stock"], 1)

            fetch.return_value.send.side_effect = send_and_check
            asyncio.run(successful.on_submit(interaction()))
            fetch.return_value.send.assert_awaited_once()
        self.assertEqual(database.get_item(item["id"])["stock"], 0)
        self.assertEqual(database.load_data()["orders"][-1]["status"], "fulfilled")


if __name__ == "__main__":
    unittest.main()