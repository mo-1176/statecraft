"""
中断した状態（Network.save_state()）を、JSONにできる形へ変える／戻す。

保存先は利用側が決める（DB、Blob、ファイル）。ここが約束するのは、
json.dumps() がそのまま通る形にすることだけ。

    bytes      → {"$bytes": "<base64>"}（添付の data など）
    tuple      → list
    それ以外の型 → TypeError（どこに入っていたかを添える）

JSONにできない値を黙って文字列にしないのは、戻した時に別の型になって
いても気づけないため（toolが返した値を、再開後のエージェントが読む）。
"""

import base64
import dataclasses
from typing import Any

from .memory import BaseMemory, DiffError, MemoryEntry, TaskEntry, TaskStatus
from .tools import ToolResult

# 保存した形の版。形を変えたら上げる。読み込みは同じ版だけを受け付ける
# （古い形を黙って読むと、欠けた項目が既定値のまま再開される）。
STATE_VERSION = 1

_BYTES = "$bytes"


def encode(value: Any, where: str) -> Any:
    """
    JSONにできる形へ。where は、変えられなかった時にどこの値かを示す文言。
    """
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, bytes):
        return {_BYTES: base64.b64encode(value).decode("ascii")}
    if isinstance(value, dict):
        return {str(k): encode(v, where) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [encode(v, where) for v in value]
    raise TypeError(
        f"中断した状態をJSONにできません。{where} に {type(value).__name__} が入っています。"
        f"toolの戻り値やsuspend()に渡す値は、文字列・数値・真偽値・None・bytesと、"
        f"それらのlist / dictで返してください。"
    )


def decode(value: Any) -> Any:
    """encodeの逆。"""
    if isinstance(value, dict):
        if set(value) == {_BYTES}:
            return base64.b64decode(value[_BYTES])
        return {k: decode(v) for k, v in value.items()}
    if isinstance(value, list):
        return [decode(v) for v in value]
    return value


# ---- ToolResult ----
def dump_result(result: ToolResult | None, where: str) -> dict | None:
    if result is None:
        return None
    return {
        "value": encode(result.value, where),
        "error": result.error,
        "execution_time": result.execution_time,
        "blobs": encode(result.blobs, where),
        "memory": result.memory,
    }


def load_result(data: dict | None) -> ToolResult | None:
    if data is None:
        return None
    return ToolResult(
        value=decode(data["value"]),
        error=data["error"],
        execution_time=data["execution_time"],
        blobs=decode(data["blobs"]),
        memory=data["memory"],
    )


# ---- 記憶 ----
def dump_memory(memory: BaseMemory) -> dict:
    """プロパティごとに、項目を辞書の配列にする。requestsのような単一値はそのまま1件。"""
    out: dict[str, Any] = {}
    for f in dataclasses.fields(memory):
        value = getattr(memory, f.name)
        if isinstance(value, list):
            out[f.name] = [_dump_entry(e) for e in value]
        else:
            out[f.name] = None if value is None else _dump_entry(value)
    return out


def restore_memory(memory: BaseMemory, data: dict) -> None:
    """
    保存した中身を、渡された実体へ書き戻す。

    差し替えずに書き戻すのは、共有の記憶は全エージェントが同じ実体を
    参照しているため（差し替えると配り直しが要る）。保存した時に無かった
    プロパティは空のまま残る。
    """
    for f in dataclasses.fields(memory):
        if f.name not in data:
            continue
        raw = data[f.name]
        if isinstance(raw, list):
            setattr(memory, f.name, [_load_entry(r) for r in raw])
        else:
            setattr(memory, f.name, None if raw is None else _load_entry(raw))


def _dump_entry(entry: MemoryEntry | TaskEntry) -> dict:
    if isinstance(entry, TaskEntry):
        return {
            "id": entry.id,
            "text": entry.text,
            "status": entry.status.value,
            "target_names": list(entry.target_names),
        }
    return {"id": entry.id, "text": entry.text}


def _load_entry(raw: dict) -> MemoryEntry | TaskEntry:
    # statusを持つのはtasksの項目だけ。
    if "status" in raw:
        return TaskEntry(
            id=raw["id"],
            text=raw["text"],
            status=TaskStatus(raw["status"]),
            target_names=list(raw["target_names"]),
        )
    return MemoryEntry(id=raw["id"], text=raw["text"])


# ---- 差分の誤り（前回反映されなかった行） ----
def dump_diff_error(error: DiffError, where: str) -> dict:
    return {
        "field": error.field,
        "entry": encode(error.entry, where),
        "message": error.message,
    }


def load_diff_error(data: dict) -> DiffError:
    return DiffError(data["field"], decode(data["entry"]), data["message"])
