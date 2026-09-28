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


def panel_interaction(title, message_id=20001, channel_id=10001, user_id=0, author_id=42):
    i = interaction(user_id)
    i.channel_id = channel_id
    i.message = SimpleNamespace(
        id=message_id, embeds=[SimpleNamespace(title=title)],
        author=SimpleNamespace(id=author_id), edit=AsyncMock(),
    )
    return i


class PersistentPanelTests(unittest.TestCase):
    def test_ready_updates_known_panels_once(self):
        bot = main.MyBot()
        with (
            patch.object(database, "get_vending_panels", return_value=[
                {"machine_name": "A"}, {"machine_name": "A"}, {"machine_name": "B"},
            ]),
            patch.object(main, "refresh_machine", new_callable=AsyncMock) as refresh,
        ):
            asyncio.run(bot.on_ready())
            asyncio.run(bot.on_ready())
            self.assertEqual(refresh.await_count, 2)
            self.assertEqual({call.args[0] for call in refresh.await_args_list}, {"A", "B"})
        asyncio.run(bot.close())

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
        bot_user = patch.object(main, "bot", SimpleNamespace(user=SimpleNamespace(id=42)))
        bot_user.start()
        self.addCleanup(bot_user.stop)

    def test_restart_dispatches_legacy_ids_without_per_machine_registration(self):
        name = "旧パネル"  # Not a configured machine at startup.
        database.add_item(name, "商品", 0, "delivery", sold_count=7)
        self.assertNotIn(name, database.get_machine_names())
        with (
            patch.object(main, "bot", main.MyBot()) as bot,
            patch.object(database, "ensure_default_templates", return_value=0),
            patch.object(database, "recover_delivery_orders"),
            patch.object(database, "get_active_orders", return_value=[]),
        ):
            bot.tree.sync = AsyncMock()
            asyncio.run(bot.setup_hook())
        bot_user = patch.object(main, "bot", SimpleNamespace(user=SimpleNamespace(id=42)))
        bot_user.start()
        self.addCleanup(bot_user.stop)
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
            i = panel_interaction(name)
            asyncio.run(button.callback(i))
            i.response.defer.assert_awaited_once_with(ephemeral=True)
            i.followup.send.assert_awaited_once()
            if action == "buy":
                self.assertIsInstance(i.followup.send.call_args.kwargs["view"], main.BuyerItemView)
            else:
                self.assertEqual(i.followup.send.call_args.kwargs["embed"].title, name)
        self.assertIsNone(main.VendingButton.__discord_ui_compiled_template__.fullmatch("vending:admin:items"))
        # A deleted/unknown machine cannot silently sell another machine's products.
        i = panel_interaction("missing", message_id=30000)
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
        i = panel_interaction("旧パネル")
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
        i = panel_interaction(name)
        asyncio.run(main.VendingButton("旧パネル", "buy").callback(i))
        self.assertEqual(i.followup.send.call_args.kwargs["view"].machine_name, name)
        database.add_item("別の自販機", "商品2", 100, "other content")
        database.save_vending_machine("別の自販機", 10001)
        i2 = panel_interaction(name)
        i2.message, i2.channel_id = i.message, i.channel_id
        asyncio.run(main.VendingButton("旧パネル", "buy").callback(i2))
        # A matching title uniquely identifies the panel even if the channel
        # hosts another machine with a different title.
        self.assertEqual(i2.followup.send.call_args.kwargs["view"].machine_name, name)
        i3 = panel_interaction("旧パネル", message_id=20002)
        asyncio.run(main.VendingButton("旧パネル", "buy").callback(i3))
        self.assertIn("利用できません", i3.response.send_message.call_args.args[0])

    def test_stale_or_ambiguous_mapping_stays_unavailable(self):
        name = "現在の自販機"
        database.add_item(name, "商品", 100, "content")
        database.save_vending_machine(name, 10001, 20001)
        for channel_id, message_id in ((10002, 20001), (10001, 20002)):
            i = panel_interaction("旧パネル", message_id, channel_id)
            asyncio.run(main.VendingButton("旧パネル", "buy").callback(i))
            self.assertIn("利用できません", i.response.send_message.call_args.args[0])
        # Matching mapping without product data must never manufacture a product.
        database.save_vending_machine("空の自販機", 10001, 30001)
        i = panel_interaction("空の自販機", 30001)
        asyncio.run(main.VendingButton("旧パネル", "buy").callback(i))
        self.assertIn("利用できません", i.response.send_message.call_args.args[0])

    def test_clicked_existing_panel_updates_in_place_and_tracks_all_messages(self):
        item = database.add_item("機械", "商品", 100, ["one", "two"])
        database.save_vending_machine("機械", 10001, 20001)
        first = panel_interaction("機械")
        second = panel_interaction("機械", message_id=20002)
        asyncio.run(main.VendingButton("機械", "buy").callback(first))
        first.message.edit.assert_awaited_once()
        self.assertIn("在庫：2個", first.message.edit.call_args.kwargs["embed"].description)
        asyncio.run(main.VendingButton("機械", "stock").callback(second))
        self.assertEqual(len(database.get_vending_panels()), 2)
        self.assertEqual(database.get_vending_machine("機械")["message_id"], 20001)
        database.update_item(item["id"], contents=["one"])
        messages = {20001: first.message, 20002: second.message}
        channel = SimpleNamespace(fetch_message=AsyncMock(side_effect=lambda mid: messages[mid]), send=AsyncMock())
        with patch.object(main, "fetch_channel", new=AsyncMock(return_value=channel)):
            asyncio.run(main.refresh_all("機械"))
        for message in messages.values():
            self.assertIn("在庫：1個", message.edit.call_args.kwargs["embed"].description)
        channel.send.assert_not_awaited()

    def test_infinite_and_legacy_stock_reflect_contents_only(self):
        finite = database.add_item("機械", "有限", 50, ["a", "b"])
        database.add_item("機械", "無限", 50, ["reuse"], unlimited=True)
        data = database.load_data()
        data["items"][0]["stock"] = 14
        data["items"][0]["legacy_stock_unregistered"] = 12
        database._save(data)
        self.assertEqual(database.get_item(finite["id"])["stock"], 2)
        embed = main.vending_embed("機械")
        self.assertIn("在庫：2個", embed.description)
        self.assertIn("在庫：無限", embed.description)
        database.update_item(finite["id"], contents=[])
        self.assertIn("在庫：0個", main.vending_embed("機械").description)

    def test_failed_refresh_never_reposts_or_deletes(self):
        database.add_item("機械", "商品", 1, "real")
        database.save_vending_machine("機械", 10001, 20001)
        for failure in (main.discord.NotFound, main.discord.Forbidden):
            response = SimpleNamespace(status=404 if failure is main.discord.NotFound else 403, reason="error")
            channel = SimpleNamespace(fetch_message=AsyncMock(side_effect=failure(response, "error")), send=AsyncMock())
            with patch.object(main, "fetch_channel", new=AsyncMock(return_value=channel)):
                self.assertIsNone(asyncio.run(main.refresh_machine("機械")))
                self.assertIsNone(asyncio.run(main.publish_machine("機械", 10001)))
            channel.send.assert_not_awaited()
            self.assertEqual(database.get_vending_machine("機械")["message_id"], 20001)

    def test_recovery_requires_owner_and_verified_bot_message(self):
        database.add_item("機械", "商品", 50, "real")
        unknown = panel_interaction("名前変更済", user_id=123)
        asyncio.run(main.VendingButton("古い名前", "buy").callback(unknown))
        self.assertIn("利用できません", unknown.response.send_message.call_args.args[0])
        self.assertNotIn("view", unknown.response.send_message.call_args.kwargs)
        owner = panel_interaction("名前変更済", user_id=main.AUTHORIZED_OWNER_ID)
        asyncio.run(main.VendingButton("古い名前", "buy").callback(owner))
        recovery = owner.followup.send.call_args.kwargs["view"]
        self.assertIsInstance(recovery, main.PanelRecoveryView)
        owner.message.author.id = 99
        recovery.children[0]._values = ["機械"]
        channel = SimpleNamespace(fetch_message=AsyncMock(return_value=owner.message))
        with patch.object(main, "fetch_channel", new=AsyncMock(return_value=channel)):
            asyncio.run(recovery.selected(owner))
        self.assertFalse(database.get_vending_panels())
        owner.message.edit.assert_not_awaited()
        # A forged title on someone else's message cannot resolve at all.
        forged = panel_interaction("機械", author_id=99)
        self.assertIsNone(main.resolve_panel_machine("機械", forged))

    def test_recovery_links_existing_product_only_and_cannot_rebind_known_panel(self):
        database.add_item("機械", "商品", 50, "real")
        database.add_item("別機械", "別商品", 50, "other")
        owner = panel_interaction("削除された名前", user_id=main.AUTHORIZED_OWNER_ID)
        asyncio.run(main.VendingButton("古い名前", "buy").callback(owner))
        recovery = owner.followup.send.call_args.kwargs["view"]
        recovery.children[0]._values = ["機械"]
        channel = SimpleNamespace(fetch_message=AsyncMock(return_value=owner.message))
        with patch.object(main, "fetch_channel", new=AsyncMock(return_value=channel)):
            asyncio.run(recovery.selected(owner))
        self.assertEqual(database.get_vending_panels()[0]["machine_name"], "機械")
        self.assertEqual(owner.message.edit.call_args.kwargs["embed"].title, "機械")
        recovery.children[0]._values = ["別機械"]
        with patch.object(main, "fetch_channel", new=AsyncMock(return_value=channel)):
            asyncio.run(recovery.selected(owner))
        self.assertEqual(database.get_vending_panels()[0]["machine_name"], "機械")
        self.assertEqual(owner.message.edit.await_count, 1)
        denied = panel_interaction("削除された名前", user_id=123)
        recovery.children[0]._values = ["別機械"]
        with patch.object(main, "fetch_channel", new=AsyncMock(return_value=channel)):
            asyncio.run(recovery.selected(denied))
        self.assertEqual(database.get_vending_panels()[0]["machine_name"], "機械")

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