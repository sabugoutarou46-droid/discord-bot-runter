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
AUTHORIZED_OWNER_ID = 1406662933458452604 # あなたのID
NOTIF_CHANNEL_ID = 1520682389653819463    # PayPay通知先
ACHIEVE_CHANNEL_ID = 1520295467227942932 # 実績看板チャンネル
LOG_CHANNEL_ID = 1520295467227942932     # 実績ログ送信先（看板と同じでOK）
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
        # データベースから全販売数を合計
        data = database.load()
        total_sales = sum(data.get("limits", {}).values())
        # 看板の名前を更新（全角数字で目立たせる）
        await channel.edit(name=f"✅実績数｜{total_sales}件")

# --- UIコンポーネント ---
class DeliveryModal(ui.Modal, title="商品の配布"):
    content = ui.TextInput(label="配布内容", style=discord.TextStyle.paragraph)
    def __init__(self, buyer_id, item_id, qty, message_id):
        super().__init__()
        self.buyer_id, self.item_id, self.qty, self.message_id = buyer_id, item_id, qty, message_id

    async def on_submit(self, interaction: discord.Interaction):
        buyer = await bot.fetch_user(self.buyer_id)
        item = next((i for i in database.get_items() if i["id"] == self.item_id), None)
        try:
            # DM送信
            await buyer.send(f"【ご購入ありがとうございます！】\n商品: {item['name']} x {self.qty}\n内容: {self.content.value}")
            # 在庫更新と制限追加
            database.update_stock(self.item_id, item['stock'] - self.qty)
            database.add_limit(str(self.buyer_id), self.item_id, self.qty, datetime.now(JST).strftime("%Y-%m-%d"))
            
            # 通知メッセージの更新
            await interaction.response.edit_message(content=f"✅ {buyer.name}さんに配布完了しました。", embed=None, view=None)
            
            # ★実績ログの送信
            log_channel = bot.get_channel(LOG_CHANNEL_ID)
            if log_channel:
                await log_channel.send(f"🎉 **実績報告**: {buyer.name}様が「{item['name']}」を購入されました！")
            
            # ★実績看板のリアルタイム更新
            await update_achievement_board()
            
        except Exception as e:
            await interaction.response.send_message(f"エラーが発生しました: {e}", ephemeral=True)

class AdminActionView(ui.View):
    def __init__(self, buyer_id, item_id, qty):
        super().__init__(timeout=None)
        self.buyer_id, self.item_id, self.qty = buyer_id, item_id, qty

    @ui.button(label="入金確認・商品を渡す", style=discord.ButtonStyle.success)
    async def deliver(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id != AUTHORIZED_OWNER_ID:
            return await interaction.response.send_message("管理者専用です。", ephemeral=True)
        await interaction.response.send_modal(DeliveryModal(self.buyer_id, self.item_id, self.qty, interaction.message.id))

# (以下、PayPayModal, QuantityModal, VendingViewなどは以前の機能を継承...)
# ※文字数の関係で主要な追加部分を強調していますが、これをベースに動作します。
