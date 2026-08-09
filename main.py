import discord
from discord.ext import commands
from discord import ui, app_commands
import database
import os
from flask import Flask
from threading import Thread
from datetime import datetime
from zoneinfo import ZoneInfo

# --- 常時起動用Webサーバー ---
app = Flask(__name__)
@app.route('/')
def home(): return "Bot is alive!"
def run(): app.run(host='0.0.0.0', port=int(os.getenv("PORT", "8080")))
def keep_alive(): Thread(target=run).start()

# --- 設定 ---
AUTHORIZED_OWNER_ID = 1406662933458452604
NOTIF_CHANNEL_ID = 1520682389653819463
ACHIEVE_CHANNEL_ID = 1520295467227942932
JST = ZoneInfo("Asia/Tokyo")

# --- Botクラス ---
class MyBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
    async def setup_hook(self):
        await self.tree.sync()
        print(f"Logged in as {self.user}")

bot = MyBot()

# --- 実績カウンター更新関数 ---
async def update_achievement_board():
    channel = bot.get_channel(ACHIEVE_CHANNEL_ID)
    if channel:
        data = database.load()
        total_sales = sum(data.get("limits", {}).values())
        try:
            await channel.edit(name=f"✅実績数｜{total_sales}件")
        except:
            pass # 頻繁な更新によるDiscordの制限回避

# --- UIコンポーネント ---
class DeliveryModal(ui.Modal, title="商品の配布"):
    content = ui.TextInput(label="配布内容", style=discord.TextStyle.paragraph)
    def __init__(self, buyer_id, item_id, qty):
        super().__init__()
        self.buyer_id, self.item_id, self.qty = buyer_id, item_id, qty

    async def on_submit(self, interaction: discord.Interaction):
        buyer = await bot.fetch_user(self.buyer_id)
        item = next((i for i in database.get_items() if i["id"] == self.item_id), None)
        try:
            await buyer.send(f"【ご購入ありがとうございます！】\n商品: {item['name']} x {self.qty}\n内容: {self.content.value}")
            database.update_stock(self.item_id, item['stock'] - self.qty)
            database.add_limit(str(self.buyer_id), self.item_id, self.qty, datetime.now(JST).strftime("%Y-%m-%d"))
            await interaction.response.edit_message(content=f"✅ {buyer.name}さんに配布完了！", embed=None, view=None)
            await update_achievement_board()
        except:
            await interaction.response.send_message("DM送信失敗", ephemeral=True)

class AdminActionView(ui.View):
    def __init__(self, buyer_id, item_id, qty):
        super().__init__(timeout=None)
        self.buyer_id, self.item_id, self.qty = buyer_id, item_id, qty
    @ui.button(label="商品を渡す", style=discord.ButtonStyle.success)
    async def deliver(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id != AUTHORIZED_OWNER_ID:
            return await interaction.response.send_message("権限がありません。", ephemeral=True)
        await interaction.response.send_modal(DeliveryModal(self.buyer_id, self.item_id, self.qty))

class PayPayModal(ui.Modal, title="PayPayリンクの入力"):
    link = ui.TextInput(label="PayPayリンク", required=True)
    def __init__(self, item, qty):
        super().__init__()
        self.item, self.qty = item, qty
    async def on_submit(self, interaction: discord.Interaction):
        channel = bot.get_channel(NOTIF_CHANNEL_ID)
        embed = discord.Embed(title="💰 新規注文", color=discord.Color.gold())
        embed.add_field(name="購入者", value=interaction.user.mention)
        embed.add_field(name="商品", value=f"{self.item['name']} x {self.qty}")
        embed.add_field(name="リンク", value=self.link.value)
        await channel.send(content="@everyone", embed=embed, view=AdminActionView(interaction.user.id, self.item['id'], self.qty))
        await interaction.response.send_message("注文送信完了！", ephemeral=True)

class QuantityModal(ui.Modal, title="購入個数"):
    qty = ui.TextInput(label="個数", default="1")
    def __init__(self, item):
        super().__init__(); self.item = item
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.send_modal(PayPayModal(self.item, int(self.qty.value)))

class VendingView(ui.View):
    def __init__(self, title):
        super().__init__(timeout=None); self.title = title
    @ui.button(label="購入する", style=discord.ButtonStyle.green)
    async def buy(self, interaction: discord.Interaction, button: ui.Button):
        items = database.get_items()
        options = [discord.SelectOption(label=f"{i['name']} ({i['price']}円)", value=str(i['id'])) for i in items if i['stock'] > 0]
        if not options: return await interaction.response.send_message("在庫なし", ephemeral=True)
        view = ui.View()
        select = ui.Select(options=options)
        async def cb(i): await i.response.send_modal(QuantityModal(next(it for it in items if str(it['id'])==select.values[0])))
        select.callback = cb
        view.add_item(select)
        await interaction.response.send_message("商品選択", view=view, ephemeral=True)

@bot.tree.command(name="vending")
async def vending(interaction: discord.Interaction, title: str = "自動販売機"):
    items = database.get_items()
    embed = discord.Embed(title=f"🥤 {title}", color=discord.Color.orange())
    for i in items: embed.add_field(name=i['name'], value=f"{i['price']}円 | 在庫:{i['stock']}", inline=False)
    await interaction.response.send_message(embed=embed, view=VendingView(title))

if __name__ == "__main__":
    keep_alive()
    bot.run(os.getenv("TOKEN1"))
