"""Discord vending bot with selectable, per-machine administration."""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime
from threading import Thread
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import discord
from discord import app_commands, ui
from discord.ext import commands
from flask import Flask

import database


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("vending-bot")
JST = ZoneInfo("Asia/Tokyo")
AUTHORIZED_OWNER_ID = 1406662933458452604
app = Flask(__name__)


@app.get("/")
def home() -> str:
    return "Bot is alive!"


@app.get("/health")
def health() -> tuple[str, int]:
    return "ok", 200


def keep_alive() -> None:
    def run_web_server() -> None:
        app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")), use_reloader=False)

    Thread(target=run_web_server, daemon=True).start()


def is_administrator(interaction: discord.Interaction) -> bool:
    return bool(
        interaction.guild
        and interaction.user.id == AUTHORIZED_OWNER_ID
    )


async def private_message(interaction: discord.Interaction, message: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


async def defer_ephemeral(interaction: discord.Interaction) -> None:
    """Acknowledge an interaction before doing any slow Discord or disk work."""
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True)


async def report_interaction_error(
    interaction: discord.Interaction,
    error: Exception,
    source: str,
) -> None:
    logger.error("Interaction failed in %s", source, exc_info=error)
    try:
        await private_message(interaction, "処理中にエラーが発生しました。もう一度お試しください。")
    except discord.HTTPException:
        logger.warning("Could not send interaction error message")


class SafeView(ui.View):
    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: ui.Item[object],
    ) -> None:
        await report_interaction_error(interaction, error, self.__class__.__name__)


class SafeModal(ui.Modal):
    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await report_interaction_error(interaction, error, self.__class__.__name__)


def truncate(value: str, length: int) -> str:
    return value if len(value) <= length else f"{value[: length - 1]}…"


def valid_http_url(value: str) -> bool:
    parsed = urlparse(value.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def parse_purchase_limit(value: str) -> int | None | bool:
    """Return None for the normal unlimited setting, or False for invalid input."""
    normalized = value.strip().lower()
    if normalized in {"", "なし", "無制限", "無"}:
        return None
    if not normalized.isdigit():
        return False
    limit = int(normalized)
    return limit if 1 <= limit <= database.MAX_PURCHASE_LIMIT else False


def positive_id(value: str) -> int | None:
    return int(value.strip()) if value.strip().isdigit() and int(value.strip()) > 0 else None


def vending_embed(machine_name: str) -> discord.Embed:
    items = database.get_items(machine_name)
    embed = discord.Embed(
        title=machine_name,
        color=discord.Color.orange(),
    )
    if not items:
        embed.description = "現在、販売中の商品はありません。"
        return embed

    product_blocks: list[str] = ["商品を選んで購入してください。"]
    for item in items:
        purchase_limit = item.get("purchase_limit")
        purchase_limit_label = "なし" if purchase_limit is None else f"{purchase_limit}個"
        product_blocks.append(
            "\n".join(
                [
                    f"**{truncate(str(item['name']), 100)}**",
                    f"値段：{item['price']}円",
                    f"在庫：{item['stock_label']}",
                    f"購入上限：{purchase_limit_label}",
                ]
            )
        )

    # Put each product in the description so Discord renders a predictable,
    # fully vertical block with a visible gap before the next product.
    description = "\n\n".join(product_blocks)
    if len(description) <= 4096:
        embed.description = description
        return embed

    # Keep all products available if a large catalog exceeds Discord's
    # description limit; non-inline fields still render one product per row.
    embed.description = product_blocks[0]
    for block in product_blocks[1:]:
        title, *details = block.splitlines()
        embed.add_field(name=title.replace("**", ""), value="\n".join(details), inline=False)
    return embed


def admin_embed() -> discord.Embed:
    return discord.Embed(
        title="自販機Bot 管理者メニュー",
        description="自販機・商品・配布内容をドロップダウンで選んで管理できます。\n管理操作は指定オーナーのみ利用できます。",
        color=discord.Color.blurple(),
    )


class MyBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self) -> None:
        added_templates = database.ensure_default_templates()
        if added_templates:
            logger.info("Added %s starter template products", added_templates)
        database.recover_delivery_orders()
        self.add_view(AdminPanelView())
        # Existing Discord messages may refer to machines absent from the current
        # config. Match their custom IDs at dispatch time rather than snapshotting
        # machine names at startup. Do not also register per-machine views: discord.py
        # dispatches both dynamic and ordinary views for the same interaction.
        self.add_dynamic_items(VendingButton)
        logger.info("Persistent vending buttons registered (existing panels supported)")
        for order in database.get_active_orders():
            self.add_view(AdminDeliveryView(order["id"]))
        await self.tree.sync()
        logger.info("Slash commands synced and persistent views registered")


bot = MyBot()


async def fetch_channel(channel_id: int) -> discord.abc.Messageable | None:
    channel = bot.get_channel(channel_id)
    if channel is not None and hasattr(channel, "send"):
        return channel
    try:
        fetched = await bot.fetch_channel(channel_id)
    except (discord.Forbidden, discord.NotFound, discord.HTTPException):
        logger.warning("Could not load channel %s", channel_id)
        return None
    return fetched if hasattr(fetched, "send") else None


async def configured_channel(key: str) -> discord.abc.Messageable | None:
    channel_id = database.get_config(key, 0)
    return await fetch_channel(int(channel_id)) if channel_id else None


async def refresh_machine(machine_name: str) -> discord.Message | None:
    saved = database.get_vending_machine(machine_name)
    if not saved:
        return None
    channel = await fetch_channel(int(saved["channel_id"]))
    if channel is None or not hasattr(channel, "send"):
        return None
    old_message = None
    if saved.get("message_id"):
        try:
            old_message = await channel.fetch_message(int(saved["message_id"]))  # type: ignore[attr-defined]
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            old_message = None

    # Send a fresh panel instead of editing in place. This intentionally moves
    # an updated vending machine to the bottom of the channel conversation.
    try:
        message = await channel.send(embed=vending_embed(machine_name), view=VendingView(machine_name))
    except (discord.Forbidden, discord.HTTPException):
        logger.warning("Could not publish vending panel in %s", machine_name)
        return None

    if old_message:
        try:
            await old_message.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            logger.warning("Could not remove the previous vending panel in %s", machine_name)

    database.save_vending_machine(machine_name, int(saved["channel_id"]), message.id)
    return message


async def publish_machine(machine_name: str, channel_id: int) -> discord.Message | None:
    old = database.get_vending_machine(machine_name)
    if old and old.get("message_id"):
        old_channel = await fetch_channel(int(old["channel_id"]))
        if old_channel is not None:
            try:
                old_message = await old_channel.fetch_message(int(old["message_id"]))  # type: ignore[attr-defined]
                await old_message.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
    channel = await fetch_channel(channel_id)
    if channel is None or not hasattr(channel, "send"):
        return None
    try:
        message = await channel.send(embed=vending_embed(machine_name), view=VendingView(machine_name))
    except (discord.Forbidden, discord.HTTPException):
        return None
    database.save_vending_machine(machine_name, channel_id, message.id)
    return message


async def refresh_all(machine_name: str) -> None:
    await refresh_machine(machine_name)


async def send_achievement_log(order: dict[str, object], buyer: discord.abc.User) -> None:
    channel = await configured_channel("achievement_channel_id")
    if channel is None:
        return
    created_at = datetime.fromisoformat(str(order["created_at"])).astimezone(JST)
    embed = discord.Embed(title="商品購入ログ", color=discord.Color.blue(), timestamp=created_at)
    embed.add_field(name="購入者", value=buyer.mention, inline=False)
    embed.add_field(name="自販機", value=str(order.get("machine_name", "自販機")), inline=False)
    embed.add_field(name="商品名", value=truncate(str(order["item_name"]), 1024), inline=False)
    embed.add_field(name="個数", value=str(order["quantity"]), inline=False)
    try:
        await channel.send(embed=embed)
    except (discord.Forbidden, discord.HTTPException):
        logger.warning("Could not send achievement log")


def channel_options(guild: discord.Guild) -> list[discord.SelectOption]:
    bot_member = guild.me
    channels = [
        channel
        for channel in guild.text_channels
        if bot_member is None or channel.permissions_for(bot_member).send_messages
    ]
    configured_ids = {
        int(database.get_config("notification_channel_id", 0) or 0),
        int(database.get_config("achievement_channel_id", 0) or 0),
    }
    configured_channels = [
        channel
        for channel in guild.text_channels
        if channel.id in configured_ids and channel not in channels
    ]
    channels = configured_channels + channels
    options = [
        discord.SelectOption(label=truncate(channel.name, 100), value=str(channel.id), description=f"#{channel.name}")
        for channel in channels[:25]
    ]
    return options or [discord.SelectOption(label="チャンネルなし", value="none")]


class ChannelSelect(ui.Select):
    def __init__(
        self,
        purpose: str,
        callback_handler: object,
        guild: discord.Guild,
        *,
        row: int = 0,
    ) -> None:
        self.purpose = purpose
        options = channel_options(guild)
        super().__init__(
            placeholder=f"{purpose}を選択",
            options=options,
            row=row,
            disabled=not any(option.value != "none" for option in options),
        )
        self.callback = callback_handler  # type: ignore[assignment]


class MachineSelect(ui.Select):
    def __init__(self, callback_handler: object, placeholder: str = "自販機を選択") -> None:
        database.ensure_default_templates()
        machines = database.get_machine_names()[:25]
        logger.info("Building machine selector '%s' with %d options: %s", placeholder, len(machines), ", ".join(machines))
        options = [discord.SelectOption(label=truncate(name, 100), value=name) for name in machines]
        options = options or [discord.SelectOption(label="自販機なし", value="none")]
        super().__init__(
            placeholder=placeholder,
            options=options,
            disabled=not any(option.value != "none" for option in options),
        )
        self.callback = callback_handler  # type: ignore[assignment]


class ItemSelect(ui.Select):
    def __init__(self, items: list[dict[str, object]], callback_handler: object | None = None, buyer: bool = False) -> None:
        # Buyers should only see purchasable inventory. Administrators must
        # still be able to select sold-out products to edit or remove them.
        available = [item for item in items if not buyer or int(item["stock"]) != 0][:25]
        options = [
            discord.SelectOption(
                label=truncate(f"{item['name']} - {item['price']}円", 100),
                description=f"在庫: {item['stock_label']}",
                value=str(item["id"]),
            )
            for item in available
        ]
        if not options:
            options = [discord.SelectOption(label="在庫なし", value="none")]
        super().__init__(
            placeholder="商品を選択",
            options=options,
            disabled=not any(option.value != "none" for option in options),
        )
        if callback_handler is not None:
            self.callback = callback_handler  # type: ignore[assignment]
        self.buyer = buyer


class MachineChannelView(SafeView):
    def __init__(self, machine_name: str, guild: discord.Guild) -> None:
        super().__init__(timeout=180)
        self.machine_name = machine_name
        self.add_item(ChannelSelect("設置先チャンネル", self.selected, guild))

    async def selected(self, interaction: discord.Interaction) -> None:
        await defer_ephemeral(interaction)
        channel_id = int(self.children[0].values[0])
        message = await publish_machine(self.machine_name, channel_id)
        await private_message(
            interaction,
            f"「{self.machine_name}」を <#{channel_id}> に設置しました。" if message else "設置できませんでした。Botの権限を確認してください。",
        )


class CreateMachineView(SafeView):
    def __init__(self, guild: discord.Guild) -> None:
        super().__init__(timeout=180)
        self.guild = guild
        self.machine_name: str | None = None
        self.add_item(ChannelSelect("設置先チャンネル", self.selected, guild))
        title_options = [
            discord.SelectOption(label="新しい題名を入力", value="new"),
            *[discord.SelectOption(label=truncate(name, 90), value=name) for name in database.get_machine_names()[:24]],
        ]
        title = ui.Select(placeholder="自販機の題名を選択", options=title_options, row=1)
        title.callback = self.title_selected
        self.add_item(title)

    async def title_selected(self, interaction: discord.Interaction) -> None:
        value = self.children[1].values[0]
        if value == "new":
            await interaction.response.send_modal(NewMachineNameModal(self))
            return
        self.machine_name = value
        await private_message(interaction, f"題名「{value}」を選択しました。設置先チャンネルも選択してください。")

    async def selected(self, interaction: discord.Interaction) -> None:
        await defer_ephemeral(interaction)
        if not self.machine_name:
            await private_message(interaction, "自販機の題名を先に選択または入力してください。")
            return
        channel_id = int(self.children[0].values[0])
        message = await publish_machine(self.machine_name, channel_id)
        await private_message(
            interaction,
            f"「{self.machine_name}」を <#{channel_id}> に設置しました。" if message else "設置できませんでした。Botの権限を確認してください。",
        )


class NewMachineNameModal(SafeModal, title="新しい自販機の題名"):
    machine_name = ui.TextInput(label="題名", max_length=80)

    def __init__(self, view: CreateMachineView) -> None:
        super().__init__()
        self.create_view = view

    async def on_submit(self, interaction: discord.Interaction) -> None:
        name = self.machine_name.value.strip()
        if not name:
            await private_message(interaction, "自販機の題名は必須です。")
            return
        self.create_view.machine_name = name
        await private_message(interaction, f"題名「{self.create_view.machine_name}」を設定しました。設置先を選択してください。")


class ProductAddMachineView(SafeView):
    def __init__(self) -> None:
        super().__init__(timeout=180)
        self.add_item(MachineSelect(self.selected, "商品を追加する自販機を選択"))

    async def selected(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(AddItemModal(self.children[0].values[0]))


class AddItemModal(SafeModal, title="商品追加"):
    name = ui.TextInput(label="商品名", max_length=100)
    price = ui.TextInput(label="価格（円・無料は0）", placeholder="450", max_length=10)
    purchase_limit = ui.TextInput(label="購入上限（なしで無制限）", placeholder="なし", max_length=3, default="なし")

    def __init__(self, machine_name: str) -> None:
        super().__init__()
        self.machine_name = machine_name

    async def on_submit(self, interaction: discord.Interaction) -> None:
        price_text = self.price.value.strip()
        limit_text = self.purchase_limit.value.strip()
        if not price_text.isdigit():
            await private_message(interaction, "価格は0以上の整数で入力してください。")
            return
        purchase_limit = parse_purchase_limit(limit_text)
        if purchase_limit is False:
            await private_message(interaction, f"購入上限は1〜{database.MAX_PURCHASE_LIMIT}個で入力してください。")
            return
        await interaction.response.send_message(
            "在庫タイプを選択してください。",
            view=StockTypeView(
                self.machine_name,
                self.name.value,
                int(price_text),
                purchase_limit,
            ),
            ephemeral=True,
        )


class StockTypeView(SafeView):
    def __init__(
        self,
        machine_name: str,
        name: str,
        price: int,
        purchase_limit: int | None = None,
        sold_count: int = 0,
        item_id: int | None = None,
        existing_contents: list[str] | None = None,
    ) -> None:
        super().__init__(timeout=180)
        self.machine_name = machine_name
        self.name = name
        self.price = price
        self.purchase_limit = purchase_limit
        self.sold_count = sold_count
        self.item_id = item_id
        self.existing_contents = existing_contents or []
        select = ui.Select(
            placeholder="在庫タイプを選択",
            options=[
                discord.SelectOption(label="有限在庫", value="finite", description="配布内容を1行につき1個として消費"),
                discord.SelectOption(label="無限在庫", value="unlimited", description="配布内容1行を繰り返し配布"),
            ],
        )
        select.callback = self.selected
        self.add_item(select)

    async def selected(self, interaction: discord.Interaction) -> None:
        unlimited = self.children[0].values[0] == "unlimited"
        await interaction.response.send_modal(
            ContentInputModal(
                self.machine_name,
                self.name,
                self.price,
                self.purchase_limit,
                self.sold_count,
                unlimited,
                self.item_id,
                self.existing_contents,
            )
        )


def content_chunks(contents: list[str], max_chunks: int = 4) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    current_length = 0
    for line in contents:
        line = str(line)
        added_length = len(line) + (1 if current else 0)
        if current and (len(current) >= 300 or current_length + added_length > 3900):
            chunks.append("\n".join(current))
            current = []
            current_length = 0
        current.append(line)
        current_length += len(line) + (1 if len(current) > 1 else 0)
    if current:
        chunks.append("\n".join(current))
    return chunks[:max_chunks] or [""]


class ContentInputModal(SafeModal, title="配布内容の登録"):
    def __init__(
        self,
        machine_name: str,
        name: str,
        price: int,
        purchase_limit: int | None,
        sold_count: int,
        unlimited: bool,
        item_id: int | None = None,
        existing_contents: list[str] | None = None,
    ) -> None:
        super().__init__()
        self.machine_name = machine_name
        self.name = name
        self.price = price
        self.purchase_limit = purchase_limit
        self.sold_count = sold_count
        self.unlimited = unlimited
        self.item_id = item_id
        values = content_chunks(existing_contents or [], 1 if unlimited else 4)
        field_count = 1 if unlimited else 4
        self.content_inputs: list[ui.TextInput] = []
        for index in range(field_count):
            field = ui.TextInput(
                label=f"配布内容 {index + 1}/{field_count}",
                placeholder="1行につき1個。空欄の欄は無視されます。",
                style=discord.TextStyle.paragraph,
                max_length=4000,
                required=False,
            )
            if index < len(values):
                field.default = values[index]
            self.content_inputs.append(field)
            self.add_item(field)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await defer_ephemeral(interaction)
        contents = "\n".join(field.value for field in self.content_inputs if field.value.strip())
        try:
            if self.item_id is None:
                item = database.add_item(
                    self.machine_name,
                    self.name,
                    self.price,
                    contents,
                    self.unlimited,
                    self.purchase_limit,
                    self.sold_count,
                )
                message = f"商品「{item['name']}」を「{self.machine_name}」に追加しました。"
            else:
                item = database.update_item(
                    self.item_id,
                    self.name,
                    self.price,
                    contents,
                    self.unlimited,
                    self.purchase_limit,
                )
                if item is None:
                    await private_message(interaction, "商品が見つかりません。")
                    return
                message = f"商品「{item['name']}」を更新しました。"
        except ValueError as error:
            await private_message(interaction, str(error))
            return
        await refresh_all(self.machine_name)
        await private_message(interaction, message)


class ProductSettingsView(SafeView):
    def __init__(self) -> None:
        super().__init__(timeout=300)
        self.add_item(MachineSelect(self.selected, "商品設定を開く自販機を選択"))

    async def selected(self, interaction: discord.Interaction) -> None:
        await defer_ephemeral(interaction)
        machine_name = self.children[0].values[0]
        view = MachineProductView(machine_name)
        embed = product_list_embed(machine_name)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)


def product_list_embed(machine_name: str) -> discord.Embed:
    items = database.get_items(machine_name)
    embed = discord.Embed(
        title=f"商品設定 / {machine_name}",
        description="商品を選択すると配布内容を確認できます。",
        color=discord.Color.blurple(),
    )
    if not items:
        embed.description = "この自販機には商品がありません。"
    for item in items[:25]:
        contents = "無限在庫（配布テンプレート1種類）" if item["unlimited"] else f"{len(item['contents'])}行の配布内容"
        purchase_limit = item.get("purchase_limit")
        purchase_limit_label = "なし" if purchase_limit is None else f"{purchase_limit}個"
        embed.add_field(
            name=truncate(str(item["name"]), 256),
            value=(
                f"値段: {item['price']}円\n"
                f"在庫: {item['stock_label']}\n"
                f"購入上限: {purchase_limit_label}\n"
                f"{contents}"
            ),
            inline=False,
        )
    return embed


class MachineProductView(SafeView):
    def __init__(self, machine_name: str) -> None:
        super().__init__(timeout=300)
        self.machine_name = machine_name
        self.add_item(ItemSelect(database.get_items(machine_name), self.selected))

    async def selected(self, interaction: discord.Interaction) -> None:
        await defer_ephemeral(interaction)
        item = database.get_item(int(self.children[0].values[0]))
        if item is None:
            await private_message(interaction, "商品が見つかりません。")
            return
        content = "\n".join(f"{index}. {line}" for index, line in enumerate(item["contents"], 1))
        if item["unlimited"]:
            content = content or "配布内容なし"
            content += "\n\n無限在庫: この1種類の内容を繰り返し配布"
        embed = discord.Embed(title=f"商品詳細 / {item['name']}", color=discord.Color.blurple())
        embed.add_field(name="自販機", value=str(item["machine_name"]), inline=True)
        embed.add_field(name="値段", value=f"{item['price']}円", inline=False)
        embed.add_field(name="在庫", value=str(item["stock_label"]), inline=False)
        purchase_limit = item.get("purchase_limit")
        embed.add_field(
            name="購入上限",
            value="なし" if purchase_limit is None else f"{purchase_limit}個",
            inline=False,
        )
        embed.add_field(name="配布内容", value=truncate(content or "配布内容なし", 4000), inline=False)
        await interaction.followup.send(embed=embed, view=ProductActionsView(item), ephemeral=True)


class ProductActionsView(SafeView):
    def __init__(self, item: dict[str, object]) -> None:
        super().__init__(timeout=300)
        self.item = item
        edit = ui.Button(label="内容・商品を編集", style=discord.ButtonStyle.primary)
        edit.callback = self.edit
        clear = ui.Button(label="配布内容を削除", style=discord.ButtonStyle.secondary)
        clear.callback = self.clear
        delete = ui.Button(label="商品そのものを削除", style=discord.ButtonStyle.danger)
        delete.callback = self.delete
        self.add_item(edit)
        self.add_item(clear)
        self.add_item(delete)

    async def edit(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(EditItemModal(self.item))

    async def clear(self, interaction: discord.Interaction) -> None:
        await defer_ephemeral(interaction)
        if database.clear_item_contents(int(self.item["id"])) is None:
            await private_message(interaction, "商品が見つかりません。")
            return
        await refresh_all(str(self.item["machine_name"]))
        await private_message(interaction, "配布内容を削除し、自販機を自動更新しました。")

    async def delete(self, interaction: discord.Interaction) -> None:
        await defer_ephemeral(interaction)
        try:
            deleted = database.delete_item(int(self.item["id"]))
        except database.OrderError as error:
            await private_message(interaction, str(error))
            return
        if not deleted:
            await private_message(interaction, "商品が見つかりません。")
            return
        await refresh_all(str(self.item["machine_name"]))
        await private_message(interaction, "商品を削除し、自販機を自動更新しました。")


class EditItemModal(SafeModal, title="商品編集"):
    name = ui.TextInput(label="商品名", max_length=100)
    price = ui.TextInput(label="価格（円・無料は0）", max_length=10)
    purchase_limit = ui.TextInput(label="購入上限（なしで無制限）", max_length=3)

    def __init__(self, item: dict[str, object]) -> None:
        super().__init__()
        self.item = item
        self.name.default = str(item["name"])
        self.price.default = str(item["price"])
        self.purchase_limit.default = str(item.get("purchase_limit") or "なし")

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not self.price.value.strip().isdigit():
            await private_message(interaction, "価格は0以上の整数で入力してください。")
            return
        limit_text = self.purchase_limit.value.strip()
        purchase_limit = parse_purchase_limit(limit_text)
        if purchase_limit is False:
            await private_message(interaction, f"購入上限は1〜{database.MAX_PURCHASE_LIMIT}個で入力してください。")
            return
        await interaction.response.send_message(
            "在庫タイプを選択してください。",
            view=StockTypeView(
                str(self.item["machine_name"]),
                self.name.value,
                int(self.price.value.strip()),
                purchase_limit,
                sold_count=int(self.item.get("sold_count", 0)),
                item_id=int(self.item["id"]),
                existing_contents=list(self.item.get("contents", [])),
            ),
            ephemeral=True,
        )


class ProductDeleteMachineView(SafeView):
    def __init__(self) -> None:
        super().__init__(timeout=180)
        self.add_item(MachineSelect(self.selected, "商品を削除する自販機を選択"))

    async def selected(self, interaction: discord.Interaction) -> None:
        await defer_ephemeral(interaction)
        await interaction.followup.send(
            "削除する商品を選択してください。",
            view=MachineProductDeleteView(self.children[0].values[0]),
            ephemeral=True,
        )


class MachineProductDeleteView(SafeView):
    def __init__(self, machine_name: str) -> None:
        super().__init__(timeout=180)
        self.machine_name = machine_name
        self.add_item(ItemSelect(database.get_items(machine_name), self.selected))

    async def selected(self, interaction: discord.Interaction) -> None:
        await defer_ephemeral(interaction)
        item = database.get_item(int(self.children[0].values[0]))
        if item is None:
            await private_message(interaction, "商品が見つかりません。")
            return
        await interaction.followup.send(
            f"「{item['name']}」を削除しますか？",
            view=ConfirmDeleteView(item),
            ephemeral=True,
        )


class ConfirmDeleteView(SafeView):
    def __init__(self, item: dict[str, object]) -> None:
        super().__init__(timeout=120)
        self.item = item
        yes = ui.Button(label="削除する", style=discord.ButtonStyle.danger)
        yes.callback = self.confirm
        no = ui.Button(label="キャンセル", style=discord.ButtonStyle.secondary)
        no.callback = self.cancel
        self.add_item(yes)
        self.add_item(no)

    async def confirm(self, interaction: discord.Interaction) -> None:
        await defer_ephemeral(interaction)
        try:
            deleted = database.delete_item(int(self.item["id"]))
        except database.OrderError as error:
            await private_message(interaction, str(error))
            return
        if deleted:
            await refresh_all(str(self.item["machine_name"]))
        await private_message(interaction, "商品を削除しました。" if deleted else "商品が見つかりません。")

    async def cancel(self, interaction: discord.Interaction) -> None:
        await private_message(interaction, "削除をキャンセルしました。")


class PurchaseModal(SafeModal, title="購入手続き"):
    quantity = ui.TextInput(label="購入個数", placeholder="1", min_length=1, max_length=3)

    def __init__(self, item: dict[str, object]) -> None:
        super().__init__()
        self.item = item
        self.quantity.default = "1"
        if int(item["price"]) > 0:
            self.paypay_link = ui.TextInput(
                label="PayPayリンク",
                placeholder="https://pay.paypay.ne.jp/...",
                max_length=500,
            )
            self.add_item(self.paypay_link)
        else:
            self.paypay_link = None

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await defer_ephemeral(interaction)
        quantity_text = self.quantity.value.strip()
        if not re.fullmatch(r"\d{1,3}", quantity_text) or int(quantity_text) <= 0:
            await private_message(interaction, "購入個数は1〜999の整数で入力してください。")
            return
        purchase_limit = self.item.get("purchase_limit")
        if purchase_limit is not None and int(quantity_text) > int(purchase_limit):
            await private_message(
                interaction,
                f"この商品の購入上限は1回につき{purchase_limit}個です。",
            )
            return
        paypay_link = self.paypay_link.value.strip() if self.paypay_link else ""
        if int(self.item["price"]) > 0 and not valid_http_url(paypay_link):
            await private_message(interaction, "PayPayリンクはhttpまたはhttpsのURLで入力してください。")
            return
        try:
            order = database.create_order(interaction.user.id, int(self.item["id"]), int(quantity_text))
        except (ValueError, database.OrderError) as error:
            await private_message(interaction, str(error))
            return

        if int(self.item["price"]) == 0:
            order = database.begin_delivery(order["id"]) or order
            try:
                buyer = await bot.fetch_user(interaction.user.id)
                await buyer.send(
                    "無料商品の配布です。\n\n"
                    f"自販機: {order['machine_name']}\n商品名: {order['item_name']}\n"
                    f"個数: {order['quantity']}個\n注文ID: {order['id']}\n\n"
                    "購入内容:\n" + "\n".join(f"・{line}" for line in order["reserved_contents"])
                )
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                database.release_order(order["id"])
                await private_message(interaction, "DM送信に失敗したため注文を取り消しました。")
                return
            database.complete_order(order["id"])
            await refresh_all(str(order["machine_name"]))
            await send_achievement_log(order, interaction.user)
            await private_message(interaction, f"無料商品をDMに送信しました。注文ID: {order['id']}")
            return

        channel = await configured_channel("notification_channel_id")
        if channel is None:
            database.release_order(order["id"])
            await private_message(interaction, "注文通知チャンネルを管理者が選択していません。")
            return
        saved_order = database.set_order_paypay_link(order["id"], paypay_link)
        if saved_order is None:
            database.release_order(order["id"])
            await private_message(interaction, "注文を保存できませんでした。")
            return
        order = saved_order
        embed = discord.Embed(title="新規注文（PayPay確認待ち）", color=discord.Color.gold())
        embed.add_field(name="注文ID", value=order["id"], inline=True)
        embed.add_field(name="購入者", value=interaction.user.mention, inline=True)
        embed.add_field(name="自販機", value=str(order["machine_name"]), inline=True)
        embed.add_field(name="商品", value=truncate(str(order["item_name"]), 100), inline=True)
        embed.add_field(name="個数", value=f"{order['quantity']}個", inline=True)
        embed.add_field(name="合計金額", value=f"{order['total_price']}円", inline=True)
        embed.add_field(name="PayPayリンク", value=truncate(paypay_link, 1024), inline=False)
        try:
            await channel.send(
                content="@everyone",
                embed=embed,
                view=AdminDeliveryView(order["id"]),
                allowed_mentions=discord.AllowedMentions(everyone=True, users=True, roles=False),
            )
        except (discord.Forbidden, discord.HTTPException):
            database.release_order(order["id"])
            await private_message(interaction, "注文通知に失敗したため、注文を取り消しました。")
            return
        await private_message(
            interaction,
            f"注文 {order['id']} を受け付けました。入金確認後に配布します。合計 {order['total_price']}円です。",
        )


class BuyerItemView(SafeView):
    def __init__(self, machine_name: str) -> None:
        super().__init__(timeout=180)
        self.machine_name = machine_name
        items = database.get_items(machine_name)
        self.items = {int(item["id"]): item for item in items}
        self.add_item(ItemSelect(items, self.selected, buyer=True))

    async def selected(self, interaction: discord.Interaction) -> None:
        item = self.items.get(int(self.children[0].values[0]))
        if item is None or item["machine_name"] != self.machine_name or int(item["stock"]) == 0:
            await private_message(interaction, "現在購入できない商品です。")
            return
        await interaction.response.send_modal(PurchaseModal(item))


class VendingButton(ui.DynamicItem[ui.Button], template=r"vending:(?P<action>buy|stock):(?P<machine>.+)"):
    """Dispatch the original public button IDs, including panels from before a restart."""

    def __init__(self, machine_name: str, action: str) -> None:
        self.machine_name = machine_name
        self.action = action
        super().__init__(
            ui.Button(
                label="購入する" if action == "buy" else "在庫確認",
                style=discord.ButtonStyle.success if action == "buy" else discord.ButtonStyle.secondary,
                custom_id=f"vending:{action}:{machine_name}",
            )
        )

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: ui.Item, match: re.Match[str]
    ) -> VendingButton:
        return cls(match["machine"], match["action"])

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            await defer_ephemeral(interaction)
            # Do not invent products for a stale or unrecognised panel. Products
            # can survive when a machine is absent from the startup name list.
            if not database.get_vending_machine(self.machine_name) and not database.get_items(self.machine_name):
                await private_message(interaction, "この自販機は現在利用できません。")
                return
            if self.action == "buy":
                await interaction.followup.send(
                    "購入する商品を選択してください。",
                    view=BuyerItemView(self.machine_name),
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(embed=vending_embed(self.machine_name), ephemeral=True)
        except Exception as error:
            # DynamicItem callbacks do not use SafeView.on_error.
            await report_interaction_error(interaction, error, "VendingButton")


class VendingView(SafeView):
    """Only buyer actions are attached to public vending panels."""

    def __init__(self, machine_name: str) -> None:
        super().__init__(timeout=None)
        self.add_item(VendingButton(machine_name, "buy"))
        self.add_item(VendingButton(machine_name, "stock"))


class SettingsView(SafeView):
    def __init__(self, guild: discord.Guild) -> None:
        super().__init__(timeout=180)
        self.add_item(ChannelSelect("PayPay注文通知チャンネル", self.notification, guild, row=0))
        self.add_item(ChannelSelect("実績チャンネル", self.achievement, guild, row=1))

    async def notification(self, interaction: discord.Interaction) -> None:
        await defer_ephemeral(interaction)
        channel_id = int(self.children[0].values[0])
        database.set_config(notification_channel_id=channel_id)
        await private_message(interaction, f"注文通知チャンネルを <#{channel_id}> に設定しました。")

    async def achievement(self, interaction: discord.Interaction) -> None:
        await defer_ephemeral(interaction)
        channel_id = int(self.children[1].values[0])
        database.set_config(achievement_channel_id=channel_id)
        await private_message(interaction, f"実績チャンネルを <#{channel_id}> に設定しました。")


class AdminPanelView(SafeView):
    def __init__(self) -> None:
        super().__init__(timeout=None)
        self.add_button("自販機作成・設置", discord.ButtonStyle.primary, self.create, 0, "vending:admin:create")
        self.add_button("自販機更新", discord.ButtonStyle.secondary, self.refresh, 0, "vending:admin:refresh")
        self.add_button("自販機を移動", discord.ButtonStyle.secondary, self.move, 0, "vending:admin:move")
        self.add_button("商品追加", discord.ButtonStyle.primary, self.add, 1, "vending:admin:add")
        self.add_button("商品設定", discord.ButtonStyle.primary, self.settings_items, 1, "vending:admin:items")
        self.add_button("商品削除", discord.ButtonStyle.danger, self.delete, 1, "vending:admin:delete")
        self.add_button("Bot設定", discord.ButtonStyle.secondary, self.settings, 2, "vending:admin:settings")

    def add_button(self, label: str, style: discord.ButtonStyle, callback: object, row: int, custom_id: str) -> None:
        button = ui.Button(label=label, style=style, custom_id=custom_id, row=row)
        button.callback = callback  # type: ignore[assignment]
        self.add_item(button)

    async def ensure_admin(self, interaction: discord.Interaction) -> bool:
        if is_administrator(interaction):
            return True
        await private_message(interaction, "管理者専用の操作です。")
        return False

    async def create(self, interaction: discord.Interaction) -> None:
        if not await self.ensure_admin(interaction) or not interaction.guild:
            return
        await defer_ephemeral(interaction)
        await interaction.followup.send("題名と設置先を選択してください。", view=CreateMachineView(interaction.guild), ephemeral=True)

    async def refresh(self, interaction: discord.Interaction) -> None:
        if not await self.ensure_admin(interaction):
            return
        await defer_ephemeral(interaction)
        await interaction.followup.send("更新する自販機を選択してください。", view=RefreshMachineView(), ephemeral=True)

    async def move(self, interaction: discord.Interaction) -> None:
        if not await self.ensure_admin(interaction):
            return
        await defer_ephemeral(interaction)
        await interaction.followup.send("移動する自販機を選択してください。", view=MoveMachineView(interaction.guild), ephemeral=True)

    async def add(self, interaction: discord.Interaction) -> None:
        if not await self.ensure_admin(interaction):
            return
        await defer_ephemeral(interaction)
        await interaction.followup.send("商品を追加する自販機を選択してください。", view=ProductAddMachineView(), ephemeral=True)

    async def settings_items(self, interaction: discord.Interaction) -> None:
        if not await self.ensure_admin(interaction):
            return
        await defer_ephemeral(interaction)
        await interaction.followup.send("商品設定を開く自販機を選択してください。", view=ProductSettingsView(), ephemeral=True)

    async def delete(self, interaction: discord.Interaction) -> None:
        if not await self.ensure_admin(interaction):
            return
        await defer_ephemeral(interaction)
        await interaction.followup.send("商品を削除する自販機を選択してください。", view=ProductDeleteMachineView(), ephemeral=True)

    async def settings(self, interaction: discord.Interaction) -> None:
        if not await self.ensure_admin(interaction) or not interaction.guild:
            return
        await defer_ephemeral(interaction)
        await interaction.followup.send("チャンネルを選択してください。", view=SettingsView(interaction.guild), ephemeral=True)


class RefreshMachineView(SafeView):
    def __init__(self) -> None:
        super().__init__(timeout=180)
        self.add_item(MachineSelect(self.selected, "更新する自販機を選択"))

    async def selected(self, interaction: discord.Interaction) -> None:
        await defer_ephemeral(interaction)
        name = self.children[0].values[0]
        message = await refresh_machine(name)
        await private_message(interaction, "自販機を更新しました。" if message else "自販機を更新できませんでした。")


class MoveMachineView(SafeView):
    def __init__(self, guild: discord.Guild | None) -> None:
        super().__init__(timeout=180)
        self.guild = guild
        # Keep the admin menu self-healing when the bot was upgraded while
        # using an existing data file or when startup seeding was interrupted.
        database.ensure_default_templates()
        self.add_item(MachineSelect(self.selected, "移動する自販機を選択"))

    async def selected(self, interaction: discord.Interaction) -> None:
        name = self.children[0].values[0]
        if self.guild:
            await interaction.response.send_message("移動先チャンネルを選択してください。", view=MachineChannelView(name, self.guild), ephemeral=True)


class AdminDeliveryView(SafeView):
    def __init__(self, order_id: str) -> None:
        super().__init__(timeout=None)
        self.order_id = order_id
        button = ui.Button(label="入金確認・配布", style=discord.ButtonStyle.success, custom_id=f"vending:deliver:{order_id}")
        button.callback = self.deliver
        self.add_item(button)

    async def deliver(self, interaction: discord.Interaction) -> None:
        if not is_administrator(interaction):
            await private_message(interaction, "管理者のみ商品を配布できます。")
            return
        # Open the modal immediately. Order lookup is performed after the
        # modal is submitted, so a slow JSON read cannot miss Discord's
        # three-second initial response deadline.
        await interaction.response.send_modal(DeliveryConfirmModal(self.order_id))


class DeliveryConfirmModal(SafeModal, title="商品の配布確認"):
    confirmation = ui.TextInput(label="入金確認済みの場合は「確認」と入力", placeholder="確認", max_length=10)

    def __init__(self, order_id: str) -> None:
        super().__init__()
        self.order_id = order_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not is_administrator(interaction):
            await private_message(interaction, "管理者のみ商品を配布できます。")
            return
        if self.confirmation.value.strip() != "確認":
            await private_message(interaction, "「確認」と入力すると配布できます。")
            return
        await defer_ephemeral(interaction)
        order = database.begin_delivery(self.order_id)
        if order is None:
            await private_message(interaction, "この注文は処理できません。")
            return
        try:
            buyer = await bot.fetch_user(int(order["buyer_id"]))
            await buyer.send(
                "ご購入ありがとうございます。\n\n"
                f"自販機: {order['machine_name']}\n商品名: {order['item_name']}\n"
                f"個数: {order['quantity']}個\n注文ID: {order['id']}\n購入金額: {order['total_price']}円\n\n"
                "購入内容:\n" + "\n".join(f"・{line}" for line in order["reserved_contents"])
            )
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            database.release_order(self.order_id)
            await private_message(interaction, "DM送信に失敗したため注文を保留に戻しました。")
            return
        database.complete_order(self.order_id)
        await refresh_all(str(order["machine_name"]))
        await send_achievement_log(order, buyer)
        await private_message(interaction, f"{buyer.mention} へ購入内容をDM送信し、注文 {self.order_id} を完了しました。")


@bot.tree.command(name="vending", description="管理者用の自販機操作パネルを開きます")
async def vending(interaction: discord.Interaction) -> None:
    if not is_administrator(interaction):
        await private_message(interaction, "この操作パネルは管理者専用です。")
        return
    await interaction.response.send_message(embed=admin_embed(), view=AdminPanelView(), ephemeral=True)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    logger.exception("Application command failed", exc_info=error)
    await private_message(interaction, "エラーが発生しました。Botのログを確認してください。")


if __name__ == "__main__":
    token = os.getenv("TOKEN1")
    logger.info("Vending data file: %s", database.DB_FILE)
    if not token:
        logger.error("TOKEN1 is not set. Add the Discord Bot Token as Railway Variable TOKEN1 to start the bot.")
        # Keep Railway's health check online while the owner finishes setup.
        # The Discord client starts automatically on the next deploy after
        # TOKEN1 is added as a Railway Variable.
        app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")), use_reloader=False)
    else:
        keep_alive()
        bot.run(token)
