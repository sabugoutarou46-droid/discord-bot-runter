# Discord Bot Runter

Discord上で商品販売・配布を行う自販機Bot。自販機ごとに商品、配布内容、価格、在庫、設置チャンネルを管理できます。

## Run & Operate

- `pnpm --filter @workspace/api-server run dev` — run the API server (port 5000)
- `pnpm run typecheck` — full typecheck across all packages
- `pnpm run build` — typecheck + build all packages
- `pnpm --filter @workspace/api-spec run codegen` — regenerate API hooks and Zod schemas from the OpenAPI spec
- `pnpm --filter @workspace/db run push` — push DB schema changes (dev only)
- Required env: `DATABASE_URL` — Postgres connection string

## Stack

- pnpm workspaces, Node.js 24, TypeScript 5.9
- API: Express 5
- DB: PostgreSQL + Drizzle ORM
- Validation: Zod (`zod/v4`), `drizzle-zod`
- API codegen: Orval (from OpenAPI spec)
- Build: esbuild (CJS bundle)

## Where things live

- `main.py` — Discord Bot本体、管理パネル、購入・配布処理、Railway用HTTPヘルスチェック
- `database.py` — `data/vending_data.json`（または `VENDING_DB_FILE`）を使った自販機・商品・注文データ管理
- `README.md` — Railwayへの配置手順、Discord権限、管理方法
- `railway.json`, `Procfile`, `runtime.txt` — Railway起動設定

## Architecture decisions

- Botトークンはコードへ保存せず、Railway Variablesの `TOKEN1` から読み込む。
- 商品は自販機名に紐づけ、有限在庫は配布内容を1行1個、無限在庫は1行のテンプレートとして保存する。
- 配布内容は1商品あたり最大1200行、購入上限は商品ごとの1回あたり個数として保存する。
- 管理操作と有料注文の配布確認は、指定されたDiscordユーザーIDに限定する。
- 商品変更時は既存パネルを編集せず、新しいパネルを投稿してチャンネルの最下部へ移動する。
- 永続データは小規模運用向けに追跡対象外のJSONファイルへ保存し、旧 `vending_data.json` は初回起動時に自動移行する。Railwayでは `/data` Volumeを利用する。

## Product

管理者は `/vending` から自販機、商品、配布内容、価格、在庫タイプ、設置・通知チャンネルを選択式で管理できます。購入者は公開パネルから商品を選び、無料商品はPayPayリンクなし、有料商品はPayPay確認後にDMで配布されます。

## User preferences

- GitHubへの公開はReplitのGitHub連携ではなく、登録したGitHub Personal Access Tokenを使ってpushする。
- GitHubリポジトリ名は `discord-bot-runter` を使う。
- Discord BotトークンのVariable名は `TOKEN1` を使う。

## Gotchas

_Populate as you build — sharp edges, "always run X before Y" rules._

## Pointers

- See the `pnpm-workspace` skill for workspace structure, TypeScript setup, and package details
