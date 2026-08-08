# discord-bot-runter

Discord上で販売・配布を行う自販機Botです。自販機ごとに設置チャンネルを選択でき、管理画面から商品・価格・配布内容・在庫タイプを管理できます。

## 主な機能

- `/vending` で管理者用パネルを開く
- 自販機の題名と設置チャンネルを選択して設置
- 自販機・商品・チャンネルをドロップダウンで選択
- 商品設定で既存商品の価格、商品名、配布内容、有限/無限在庫を編集
- 商品の配布内容だけを削除、または商品そのものを削除
- 商品を編集・削除すると、自販機パネルを自動更新してチャンネル最下部へ再投稿
- 無料商品（価格 `0`）はPayPayリンクなしで購入でき、購入内容をDMで自動配布
- 無限在庫は配布内容を1行だけ保存し、繰り返し配布
- `/health` と `/` のHTTPエンドポイントでRailwayの稼働確認に対応

## Railwayでの設定

1. このリポジトリをRailwayに接続します。
2. Variablesに次の名前でDiscord Bot Tokenを登録します。

   `TOKEN1`

3. Deployします。起動コマンドは `python main.py` です。
4. Discord Developer PortalでBotをサーバーへ招待し、少なくとも次の権限を付与します。
   - View Channels
   - Send Messages
   - Embed Links
   - Read Message History
   - Manage Messages
   - Mention Everyone（注文通知で`@everyone`を使う場合）

Bot Tokenはソースコード、`.env`、`vending_data.json`へ保存しないでください。

## データ保存について

初期状態では `vending_data.json` に保存します。Railwayの再デプロイやサービス再作成でデータを失わないよう、Railway Volumeを `/data` にマウントし、Variableを次のように設定してください。

`VENDING_DB_FILE=/data/vending_data.json`

## 管理の流れ

1. Botをサーバーへ招待
2. `/vending` を実行
3. 「自販機作成・設置」で題名とチャンネルを選択
4. 「商品追加」で自販機を選択し、商品情報を登録
5. 「Bot設定」で注文通知チャンネルと実績チャンネルを選択

有料商品は購入者がPayPayリンクを入力し、管理者が注文通知の「入金確認・配布」を押してDM配布します。無料商品はPayPayリンク入力なしで即時DM配布されます。