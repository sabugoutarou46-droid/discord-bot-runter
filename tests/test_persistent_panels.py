"""Offline regression tests for buttons on messages that predate a bot restart."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import database
import main


def interaction(user_id=0):
    response = SimpleNamespace(is_done=lambda: False, defer=AsyncMock(), send_message=AsyncMock())
    return SimpleNamespace(
        guild=object(), user=SimpleNamespace(id=user_id),
        response=response, followup=SimpleNamespace(send=AsyncMock()),
    )


class PersistentPanelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patches = [
            patch.object(database, "DB_FILE", str(Path(self.temp.name) / "vending.json")),
            patch.object(database, "BACKUP_FILE", str(Path(self.temp.name) / "vending.json.bak")),
            patch.object(database, "LEGACY_DB_FILE", str(Path(self.temp.name) / "legacy.json")),
        ]
        for setting in patches:
            setting.start()
            self.addCleanup(setting.stop)

    def test_restart_dispatches_legacy_ids_without_per_machine_registration(self):
        name = "旧パネル"  # Not a configured machine at startup.
        database.add_item(name, "商品", 0, "delivery", sold_count=7)
        self.assertNotIn(name, database.get_machine_names())
        with (
            patch.object(database, "ensure_default_templates", return_value=0),
            patch.object(database, "recover_delivery_orders"),
            patch.object(database, "get_active_orders", return_value=[]),
        ):
            bot = main.MyBot()
            bot.tree.sync = AsyncMock()
            asyncio.run(bot.setup_hook())
        store = bot._connection._view_store
        self.assertEqual(list(store._dynamic_items.values()), [main.VendingButton])
        self.assertNotIn((2, f"vending:buy:{name}"), store._views.get(None, {}))
        self.assertIn((2, "vending:admin:items"), store._views.get(None, {}))
        bot.add_view(main.VendingView(name))
        self.assertNotIn((2, f"vending:buy:{name}"), store._views.get(None, {}))
        for action in ("buy", "stock"):
            custom_id = f"vending:{action}:{name}"
            match = main.VendingButton.__discord_ui_compiled_template__.fullmatch(custom_id)
            self.assertIsNotNone(match)
            button = asyncio.run(main.VendingButton.from_custom_id(interaction(), None, match))
            i = interaction()
            asyncio.run(button.callback(i))
            i.response.defer.assert_awaited_once_with(ephemeral=True)
            i.followup.send.assert_awaited_once()
            if action == "buy":
                self.assertIsInstance(i.followup.send.call_args.kwargs["view"], main.BuyerItemView)
            else:
                self.assertEqual(i.followup.send.call_args.kwargs["embed"].title, name)
        self.assertIsNone(main.VendingButton.__discord_ui_compiled_template__.fullmatch("vending:admin:items"))
        # A deleted/unknown machine cannot silently sell another machine's products.
        i = interaction()
        asyncio.run(main.VendingButton("missing", "buy").callback(i))
        self.assertIn("利用できません", i.response.send_message.call_args.args[0])
        asyncio.run(bot.close())

    def test_new_panel_has_only_dynamic_handlers_and_original_ids(self):
        view = main.VendingView("再起動:前")
        self.assertEqual(
            [item.custom_id for item in view.children],
            ["vending:buy:再起動:前", "vending:stock:再起動:前"],
        )
        self.assertTrue(all(isinstance(item, main.VendingButton) for item in view.children))

    def test_renamed_panel_resolves_by_saved_message_and_channel_without_new_items(self):
        name = "現在の自販機"
        item = database.add_item(name, "垢①Steamなし", 100, "real delivery")
        database.save_vending_machine(name, 10001, 20001)
        i = interaction()
        i.message = SimpleNamespace(id=20001, embeds=[SimpleNamespace(title="旧パネル")])
        i.channel_id = 10001
        asyncio.run(main.VendingButton("旧パネル", "buy").callback(i))
        view = i.followup.send.call_args.kwargs["view"]
        self.assertEqual(view.machine_name, name)
        self.assertEqual(list(view.items), [item["id"]])
        self.assertEqual(database.get_item(item["id"])["contents"], ["real delivery"])
        asyncio.run(main.VendingButton("旧パネル", "stock").callback(i))
        self.assertEqual(i.followup.send.call_args.kwargs["embed"].title, name)

    def test_channel_fallback_requires_unique_machine_matching_embedded_title(self):
        name = "現在の自販機"
        database.add_item(name, "商品", 100, "content")
        database.save_vending_machine(name, 10001)
        i = interaction()
        i.message = SimpleNamespace(id=20001, embeds=[SimpleNamespace(title=name)])
        i.channel_id = 10001
        asyncio.run(main.VendingButton("旧パネル", "buy").callback(i))
        self.assertEqual(i.followup.send.call_args.kwargs["view"].machine_name, name)
        database.add_item("別の自販機", "商品2", 100, "other content")
        database.save_vending_machine("別の自販機", 10001)
        i2 = interaction()
        i2.message, i2.channel_id = i.message, i.channel_id
        asyncio.run(main.VendingButton("旧パネル", "buy").callback(i2))
        # A matching title uniquely identifies the panel even if the channel
        # hosts another machine with a different title.
        self.assertEqual(i2.followup.send.call_args.kwargs["view"].machine_name, name)
        i3 = interaction()
        i3.message = SimpleNamespace(id=20002, embeds=[SimpleNamespace(title="旧パネル")])
        i3.channel_id = 10001
        asyncio.run(main.VendingButton("旧パネル", "buy").callback(i3))
        self.assertIn("利用できません", i3.response.send_message.call_args.args[0])

    def test_stale_or_ambiguous_mapping_stays_unavailable(self):
        name = "現在の自販機"
        database.add_item(name, "商品", 100, "content")
        database.save_vending_machine(name, 10001, 20001)
        for channel_id, message_id in ((10002, 20001), (10001, 20002)):
            i = interaction()
            i.channel_id = channel_id
            i.message = SimpleNamespace(id=message_id, embeds=[SimpleNamespace(title=name)])
            asyncio.run(main.VendingButton("旧パネル", "buy").callback(i))
            i.followup.send.assert_not_awaited()
            self.assertIn("利用できません", i.response.send_message.call_args.args[0])
        # Matching mapping without product data must never manufacture a product.
        database.save_vending_machine("空の自販機", 10001, 30001)
        i = interaction()
        i.channel_id = 10001
        i.message = SimpleNamespace(id=30001, embeds=[SimpleNamespace(title="空の自販機")])
        asyncio.run(main.VendingButton("旧パネル", "buy").callback(i))
        self.assertIn("利用できません", i.response.send_message.call_args.args[0])

    def test_admin_product_settings_button_is_authorized_and_opens_menu(self):
        view = main.AdminPanelView()
        button = next(item for item in view.children if item.custom_id == "vending:admin:items")
        denied = interaction(123)
        asyncio.run(button.callback(denied))
        denied.followup.send.assert_not_awaited()
        self.assertIn("管理者専用", denied.response.send_message.call_args.args[0])
        allowed = interaction(main.AUTHORIZED_OWNER_ID)
        with patch.object(database, "ensure_default_templates", return_value=0):
            asyncio.run(button.callback(allowed))
        allowed.response.defer.assert_awaited_once_with(ephemeral=True)
        self.assertIsInstance(allowed.followup.send.call_args.kwargs["view"], main.ProductSettingsView)

    def test_edit_keeps_hidden_sold_count_even_with_stale_modal_value(self):
        item = database.add_item("機械", "old", 5, "secret", sold_count=42)
        modal = main.ContentInputModal("機械", "new", 10, None, 0, False, item_id=item["id"])
        modal.content_inputs[0]._value = "new secret"
        i = interaction()
        with patch.object(main, "refresh_all", new_callable=AsyncMock):
            asyncio.run(modal.on_submit(i))
        saved = database.get_item(item["id"])
        self.assertEqual(saved["sold_count"], 42)
        self.assertEqual(saved["contents"], ["new secret"])
        self.assertEqual(saved["name"], "new")


if __name__ == "__main__":
    unittest.main()