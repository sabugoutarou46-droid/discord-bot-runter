"""Durable JSON storage for the Discord vending bot.

The storage model is intentionally small and portable.  Products belong to a
named vending machine, and finite inventory is represented by one delivery
line per unit.  Unlimited products keep one delivery template and can be sold
repeatedly without consuming it.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import threading
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo


LEGACY_DB_FILE = "vending_data.json"
DEFAULT_DB_FILE = os.path.join("data", "vending_data.json")
DB_FILE = os.getenv("VENDING_DB_FILE") or DEFAULT_DB_FILE
BACKUP_FILE = f"{DB_FILE}.bak"
DEFAULT_MACHINE_NAME = "自販機"
DEFAULT_NOTIFICATION_CHANNEL_ID = 1520682389653819463
DEFAULT_ACHIEVEMENT_CHANNEL_ID = 1520295467227942932
MAX_CONTENT_LINES = 1200
MAX_PURCHASE_LIMIT = 999
SCHEMA_VERSION = 7
JST = ZoneInfo("Asia/Tokyo")
_lock = threading.RLock()
_UNSET = object()


class OrderError(ValueError):
    """An error safe to show to a Discord user."""


def _default_data() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "next_item_id": 1,
        "next_order_id": 1,
        "items": [],
        "orders": [],
        "config": {
            "admin_role_id": None,
            "notification_channel_id": DEFAULT_NOTIFICATION_CHANNEL_ID,
            "achievement_channel_id": DEFAULT_ACHIEVEMENT_CHANNEL_ID,
            "daily_purchase_limit": 0,
            "vending_machines": {},
        },
    }


def _clean_contents(value: Any, *, allow_unlimited_marker: bool = False) -> list[str]:
    if isinstance(value, str):
        raw_lines = value.splitlines()
    elif isinstance(value, list):
        raw_lines = value
    else:
        raw_lines = []

    lines: list[str] = []
    for raw_line in raw_lines:
        line = str(raw_line).strip()
        if not line:
            continue
        if allow_unlimited_marker and line == "無限":
            continue
        if len(line) > 1000:
            raise ValueError("配布内容の1行は1000文字以内で入力してください。")
        lines.append(line)
    if len(lines) > MAX_CONTENT_LINES:
        raise ValueError(f"配布内容は1商品につき{MAX_CONTENT_LINES}行まで登録できます。")
    if sum(len(line) for line in lines) > 1_200_000:
        raise ValueError("配布内容の合計が大きすぎます。")
    return lines


def _stock(item: dict[str, Any]) -> int:
    return -1 if item.get("unlimited") else len(item.get("contents", []))


def _public_item(item: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(item)
    result["contents"] = list(result.get("contents", []))
    result["unlimited"] = bool(result.get("unlimited", False))
    result["stock"] = _stock(result)
    result["stock_label"] = "無限" if result["unlimited"] else f"{result['stock']}個"
    return result


def _normalise_data(data: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    changed = False
    defaults = _default_data()
    for key, value in defaults.items():
        if key not in data:
            data[key] = copy.deepcopy(value)
            changed = True

    if not isinstance(data.get("items"), list):
        data["items"] = []
        changed = True
    if not isinstance(data.get("orders"), list):
        data["orders"] = []
        changed = True
    if not isinstance(data.get("config"), dict):
        data["config"] = copy.deepcopy(defaults["config"])
        changed = True

    config = data["config"]
    for key, value in defaults["config"].items():
        if key not in config:
            config[key] = copy.deepcopy(value)
            changed = True
    if not isinstance(config.get("vending_machines"), dict):
        config["vending_machines"] = {}
        changed = True
    if not config.get("notification_channel_id"):
        config["notification_channel_id"] = DEFAULT_NOTIFICATION_CHANNEL_ID
        changed = True
    if not config.get("achievement_channel_id"):
        config["achievement_channel_id"] = DEFAULT_ACHIEVEMENT_CHANNEL_ID
        changed = True
    if config.get("daily_purchase_limit") == 1:
        # This was the old hidden default, not a user-configurable product
        # limit. Product purchase limits are now unlimited unless specified.
        config["daily_purchase_limit"] = 0
        changed = True

    # Migrate the old single-panel format.
    if config.get("vending_channel_id") and config.get("vending_message_id"):
        config["vending_machines"].setdefault(
            DEFAULT_MACHINE_NAME,
            {
                "channel_id": config["vending_channel_id"],
                "message_id": config["vending_message_id"],
            },
        )
        config.pop("vending_channel_id", None)
        config.pop("vending_message_id", None)
        changed = True

    for item in data["items"]:
        if not isinstance(item, dict):
            continue
        if "machine_name" not in item:
            item["machine_name"] = DEFAULT_MACHINE_NAME
            changed = True
        if "contents" not in item:
            old_stock = max(int(item.get("stock", 0) or 0), 0)
            item["contents"] = []
            item["legacy_stock_unregistered"] = old_stock
            changed = True
        else:
            normalized = _clean_contents(item.get("contents", []))
            if normalized != item.get("contents"):
                item["contents"] = normalized
                changed = True
        if "unlimited" not in item:
            item["unlimited"] = False
            changed = True
        purchase_limit = item.get("purchase_limit")
        if data.get("schema_version", 0) < SCHEMA_VERSION and purchase_limit == 1:
            # Version 6 supplied 1 as an implicit default. Version 7 makes
            # the normal product setting unlimited, so migrate that default.
            item["purchase_limit"] = None
            purchase_limit = None
            changed = True
        if purchase_limit == 0 or purchase_limit == "":
            item["purchase_limit"] = None
            changed = True
        elif purchase_limit is not None and (
            not isinstance(purchase_limit, int)
            or isinstance(purchase_limit, bool)
            or not 1 <= int(purchase_limit) <= MAX_PURCHASE_LIMIT
        ):
            item["purchase_limit"] = None
            changed = True
        if item["unlimited"] and len(item["contents"]) > 1:
            item["contents"] = item["contents"][:1]
            changed = True
        if "legacy_stock_unregistered" not in item:
            item["legacy_stock_unregistered"] = 0
            changed = True
        item.pop("stock", None)

    for order in data["orders"]:
        if isinstance(order, dict) and "reserved_contents" not in order:
            order["reserved_contents"] = []
            changed = True

    max_item_id = max(
        (int(item.get("id", 0)) for item in data["items"] if str(item.get("id", "")).isdigit()),
        default=0,
    )
    if not isinstance(data.get("next_item_id"), int) or data["next_item_id"] <= max_item_id:
        data["next_item_id"] = max_item_id + 1
        changed = True

    max_order_id = max(
        (
            int(str(order.get("id", "")).removeprefix("ORD-"))
            for order in data["orders"]
            if str(order.get("id", "")).removeprefix("ORD-").isdigit()
        ),
        default=0,
    )
    if not isinstance(data.get("next_order_id"), int) or data["next_order_id"] <= max_order_id:
        data["next_order_id"] = max_order_id + 1
        changed = True
    if data.get("schema_version") != SCHEMA_VERSION:
        data["schema_version"] = SCHEMA_VERSION
        changed = True
    return data, changed


def _write_data(data: dict[str, Any]) -> None:
    directory = os.path.dirname(os.path.abspath(DB_FILE)) or "."
    os.makedirs(directory, exist_ok=True)
    temporary_file = f"{DB_FILE}.tmp"
    with open(temporary_file, "w", encoding="utf-8") as file:
        json.dump(data, file, indent=2, ensure_ascii=False)
        file.flush()
        os.fsync(file.fileno())
    if os.path.exists(DB_FILE):
        try:
            _read_json_file(DB_FILE)
        except (json.JSONDecodeError, OSError, ValueError):
            # Keep an existing good backup when recovering from a damaged file.
            pass
        else:
            shutil.copyfile(DB_FILE, BACKUP_FILE)
    os.replace(temporary_file, DB_FILE)


def _read_json_file(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as file:
        data = json.load(file)
    if not isinstance(data, dict):
        raise ValueError("保存データの形式が正しくありません。")
    return data


def _restore_source() -> tuple[str, dict[str, Any]] | None:
    """Find a previous data file without silently replacing it with defaults."""
    candidates = [BACKUP_FILE]
    if os.path.abspath(LEGACY_DB_FILE) != os.path.abspath(DB_FILE):
        candidates.append(LEGACY_DB_FILE)
    for path in candidates:
        if not os.path.exists(path):
            continue
        try:
            return path, _read_json_file(path)
        except (json.JSONDecodeError, OSError, ValueError):
            continue
    return None


def load_data() -> dict[str, Any]:
    with _lock:
        if not os.path.exists(DB_FILE):
            restored = _restore_source()
            data = restored[1] if restored else _default_data()
            data, changed = _normalise_data(data)
            _write_data(data)
            return copy.deepcopy(data)
        try:
            data = _read_json_file(DB_FILE)
        except (json.JSONDecodeError, OSError, ValueError) as error:
            restored = _restore_source()
            if restored is None:
                raise RuntimeError(f"{DB_FILE} を読み込めません。バックアップも見つかりません。") from error
            _, data = restored
            data, _ = _normalise_data(data)
            _write_data(data)
            return copy.deepcopy(data)
        data, changed = _normalise_data(data)
        if changed:
            _write_data(data)
        return copy.deepcopy(data)


def _save(data: dict[str, Any]) -> None:
    with _lock:
        normalized, _ = _normalise_data(copy.deepcopy(data))
        _write_data(normalized)


def get_config(key: str, default: Any = None) -> Any:
    return load_data()["config"].get(key, default)


def set_config(**values: Any) -> None:
    data = load_data()
    for key, value in values.items():
        if key in data["config"]:
            data["config"][key] = value
    _save(data)


def get_vending_machines() -> dict[str, dict[str, int | None]]:
    machines = load_data()["config"].get("vending_machines", {})
    return {
        str(name): {
            "channel_id": int(value["channel_id"]),
            "message_id": int(value["message_id"]) if value.get("message_id") else None,
        }
        for name, value in machines.items()
        if isinstance(value, dict) and value.get("channel_id")
    }


def get_vending_machine(name: str) -> dict[str, int | None] | None:
    return get_vending_machines().get(name)


def save_vending_machine(name: str, channel_id: int, message_id: int | None = None) -> None:
    name = str(name).strip()
    if not name or len(name) > 80:
        raise ValueError("自販機の題名は1〜80文字で入力してください。")
    if channel_id <= 0:
        raise ValueError("チャンネル情報が正しくありません。")
    data = load_data()
    data["config"]["vending_machines"][name] = {
        "channel_id": channel_id,
        "message_id": message_id,
    }
    _save(data)


def delete_vending_machine(name: str) -> bool:
    data = load_data()
    if name not in data["config"]["vending_machines"]:
        return False
    del data["config"]["vending_machines"][name]
    data["items"] = [item for item in data["items"] if item.get("machine_name") != name]
    _save(data)
    return True


def get_machine_names() -> list[str]:
    names = list(get_vending_machines().keys())
    if not names:
        names = [DEFAULT_MACHINE_NAME]
    return names


def get_items(machine_name: str | None = None) -> list[dict[str, Any]]:
    items = load_data()["items"]
    if machine_name is not None:
        items = [item for item in items if item.get("machine_name") == machine_name]
    return [_public_item(item) for item in items]


def get_item(item_id: int) -> dict[str, Any] | None:
    return next((item for item in get_items() if item["id"] == item_id), None)


def add_item(
    machine_name: str,
    name: str,
    price: int,
    contents: list[str] | str,
    unlimited: bool = False,
    purchase_limit: int | None = None,
) -> dict[str, Any]:
    machine_name = str(machine_name).strip()
    name = str(name).strip()
    if not machine_name:
        raise ValueError("自販機を選択してください。")
    if not name or len(name) > 100:
        raise ValueError("商品名は1〜100文字で入力してください。")
    if not isinstance(price, int) or isinstance(price, bool) or price < 0:
        raise ValueError("価格は0以上の整数で入力してください。")
    if purchase_limit is not None and (
        not isinstance(purchase_limit, int)
        or isinstance(purchase_limit, bool)
        or not 1 <= purchase_limit <= MAX_PURCHASE_LIMIT
    ):
        raise ValueError(f"購入上限は1〜{MAX_PURCHASE_LIMIT}個で設定してください。")
    clean_lines = _clean_contents(contents)
    if unlimited and len(clean_lines) > 1:
        clean_lines = clean_lines[:1]
    if unlimited and not clean_lines:
        raise ValueError("無限在庫には配布内容を1行登録してください。")
    if not unlimited and not clean_lines:
        raise ValueError("有限在庫には配布内容を1行以上登録してください。")

    data = load_data()
    item = {
        "id": data["next_item_id"],
        "machine_name": machine_name,
        "name": name,
        "price": price,
        "contents": clean_lines,
        "unlimited": bool(unlimited),
        "purchase_limit": purchase_limit,
        "legacy_stock_unregistered": 0,
    }
    data["next_item_id"] += 1
    data["items"].append(item)
    _save(data)
    return _public_item(item)


def update_item(
    item_id: int,
    name: str | None = None,
    price: int | None = None,
    contents: list[str] | str | None = None,
    unlimited: bool | None = None,
    purchase_limit: int | None | object = _UNSET,
) -> dict[str, Any] | None:
    if name is not None:
        name = str(name).strip()
        if not name or len(name) > 100:
            raise ValueError("商品名は1〜100文字で入力してください。")
    if price is not None and (not isinstance(price, int) or isinstance(price, bool) or price < 0):
        raise ValueError("価格は0以上の整数で入力してください。")
    if purchase_limit is not _UNSET and purchase_limit is not None and (
        not isinstance(purchase_limit, int)
        or isinstance(purchase_limit, bool)
        or not 1 <= purchase_limit <= MAX_PURCHASE_LIMIT
    ):
        raise ValueError(f"購入上限は1〜{MAX_PURCHASE_LIMIT}個で設定してください。")
    clean_lines = _clean_contents(contents) if contents is not None else None

    data = load_data()
    item = next((entry for entry in data["items"] if entry["id"] == item_id), None)
    if item is None:
        return None
    target_unlimited = item.get("unlimited", False) if unlimited is None else unlimited
    if clean_lines is not None:
        if target_unlimited and len(clean_lines) > 1:
            clean_lines = clean_lines[:1]
        if target_unlimited and not clean_lines:
            raise ValueError("無限在庫には配布内容を1行登録してください。")
    if name is not None:
        item["name"] = name
    if price is not None:
        item["price"] = price
    if unlimited is not None:
        item["unlimited"] = bool(unlimited)
    if purchase_limit is not _UNSET:
        item["purchase_limit"] = purchase_limit
    if clean_lines is not None:
        item["contents"] = clean_lines
        item["legacy_stock_unregistered"] = 0
    if item.get("unlimited") and len(item.get("contents", [])) > 1:
        item["contents"] = item["contents"][:1]
    _save(data)
    return _public_item(item)


def clear_item_contents(item_id: int) -> dict[str, Any] | None:
    data = load_data()
    item = next((entry for entry in data["items"] if entry["id"] == item_id), None)
    if item is None:
        return None
    item["contents"] = []
    item["unlimited"] = False
    item["legacy_stock_unregistered"] = 0
    _save(data)
    return _public_item(item)


def delete_item(item_id: int) -> bool:
    data = load_data()
    if not any(item["id"] == item_id for item in data["items"]):
        return False
    if any(
        order["item_id"] == item_id and order.get("status") in {"pending", "delivering"}
        for order in data["orders"]
    ):
        raise OrderError("処理中の注文があるため、この商品は削除できません。")
    data["items"] = [item for item in data["items"] if item["id"] != item_id]
    _save(data)
    return True


def _today_jst() -> str:
    return datetime.now(timezone.utc).astimezone(JST).date().isoformat()


def create_order(buyer_id: int, item_id: int, quantity: int) -> dict[str, Any]:
    if not isinstance(buyer_id, int) or buyer_id <= 0:
        raise OrderError("購入者情報が正しくありません。")
    if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity <= 0:
        raise OrderError("個数は1以上の整数で入力してください。")

    data = load_data()
    item = next((entry for entry in data["items"] if entry["id"] == item_id), None)
    if item is None:
        raise OrderError("商品が見つかりません。")
    contents = _clean_contents(item.get("contents", []))
    unlimited = bool(item.get("unlimited", False))
    purchase_limit = item.get("purchase_limit")
    if purchase_limit is not None and quantity > int(purchase_limit):
        raise OrderError(f"この商品の購入上限は1回につき{purchase_limit}個です。")
    if unlimited:
        if not contents:
            raise OrderError("この商品は現在準備中です。")
        reserved_contents = [contents[0]] * quantity
    else:
        if len(contents) < quantity:
            raise OrderError(f"在庫が足りません。現在の在庫: {len(contents)}個")
        reserved_contents = contents[:quantity]

    daily_limit = int(data["config"].get("daily_purchase_limit", 1))
    today = _today_jst()
    purchased_today = sum(
        1
        for order in data["orders"]
        if order.get("buyer_id") == buyer_id
        and order.get("purchase_date") == today
        and order.get("status") in {"pending", "delivering", "fulfilled"}
    )
    if daily_limit > 0 and purchased_today >= daily_limit:
        raise OrderError(f"1日の購入回数上限（{daily_limit}回）に達しています。")

    order_id = f"ORD-{data['next_order_id']:08d}"
    data["next_order_id"] += 1
    if not unlimited:
        item["contents"] = contents[quantity:]
    order = {
        "id": order_id,
        "buyer_id": buyer_id,
        "item_id": item_id,
        "machine_name": item.get("machine_name", DEFAULT_MACHINE_NAME),
        "item_name": item["name"],
        "unit_price": item["price"],
        "quantity": quantity,
        "total_price": item["price"] * quantity,
        "paypay_link": "",
        "purchase_date": today,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "reserved_contents": reserved_contents,
        "status": "pending",
    }
    data["orders"].append(order)
    _save(data)
    return copy.deepcopy(order)


def get_order(order_id: str) -> dict[str, Any] | None:
    order = next((entry for entry in load_data()["orders"] if entry["id"] == order_id), None)
    return copy.deepcopy(order) if order else None


def get_active_orders() -> list[dict[str, Any]]:
    return [
        copy.deepcopy(order)
        for order in load_data()["orders"]
        if order.get("status") in {"pending", "delivering"}
    ]


def recover_delivery_orders() -> int:
    data = load_data()
    recovered = 0
    for order in data["orders"]:
        if order.get("status") == "delivering":
            order["status"] = "pending"
            recovered += 1
    if recovered:
        _save(data)
    return recovered


def set_order_paypay_link(order_id: str, paypay_link: str) -> dict[str, Any] | None:
    data = load_data()
    order = next((entry for entry in data["orders"] if entry["id"] == order_id), None)
    if order is None or order.get("status") != "pending":
        return None
    order["paypay_link"] = paypay_link
    _save(data)
    return copy.deepcopy(order)


def begin_delivery(order_id: str) -> dict[str, Any] | None:
    data = load_data()
    order = next((entry for entry in data["orders"] if entry["id"] == order_id), None)
    if order is None or order.get("status") != "pending":
        return None
    order["status"] = "delivering"
    _save(data)
    return copy.deepcopy(order)


def complete_order(order_id: str) -> bool:
    data = load_data()
    order = next((entry for entry in data["orders"] if entry["id"] == order_id), None)
    if order is None or order.get("status") != "delivering":
        return False
    order["status"] = "fulfilled"
    order["fulfilled_at"] = datetime.now(timezone.utc).isoformat()
    _save(data)
    return True


def release_order(order_id: str) -> bool:
    data = load_data()
    order = next((entry for entry in data["orders"] if entry["id"] == order_id), None)
    if order is None or order.get("status") not in {"pending", "delivering"}:
        return False
    item = next((entry for entry in data["items"] if entry["id"] == order["item_id"]), None)
    if item is not None and not item.get("unlimited"):
        item["contents"] = list(order.get("reserved_contents", [])) + list(item.get("contents", []))
    order["status"] = "cancelled"
    order["cancelled_at"] = datetime.now(timezone.utc).isoformat()
    _save(data)
    return True