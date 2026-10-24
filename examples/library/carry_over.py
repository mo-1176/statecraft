"""
次のターンへ引き継ぐ記憶。

引き継ぎの仕組みはフレームワーク側に無い。何をセッションとみなすかは
アプリケーションごとに違うため。
"""

import json
from pathlib import Path
from typing import ClassVar, Self

from statecraft import MemoryEntry, Network


class CarryOver:
    # 引き継ぐプロパティ。基準は「今回の質問に依存しないか」。
    # facts / actions / decisions を入れない理由は docs/sampleの読み方.md にある。
    # 絞るのは保存する側で行う（保存しなければ引き継がれようがない）。
    FIELDS: ClassVar[tuple[str, ...]] = ("ontology", "vars")

    def __init__(self, rows: dict[str, list[dict]] | None = None) -> None:
        self.rows = {name: list((rows or {}).get(name) or []) for name in self.FIELDS}

    @classmethod
    def capture(cls, net: Network) -> Self:
        """終わったターンの共有記憶から、引き継ぐものだけを取り出す。"""
        # ontology は蓄積するほど価値が上がるが、初期トークンも押し上げる。
        # 多くなってきたらここで別途LLMへ投げて統合・圧縮する。
        return cls(
            {
                name: [{"id": e.id, "text": e.text} for e in getattr(net.shared_memory, name)]
                for name in cls.FIELDS
            }
        )

    def apply(self, net: Network) -> None:
        """新しいターンのNetworkへ入れる。"""
        # Network構築後に代入してよい。全Agentが同じ SharedMemory
        # インスタンスを参照しているため、後から入れても全員に反映される。
        for name, rows in self.rows.items():
            if rows:
                setattr(net.shared_memory, name, [MemoryEntry(id=r["id"], text=r["text"]) for r in rows])

    def summary(self) -> str:
        """何を持ち越すかの一覧（例: ontology 2件 / vars 1件）。空なら空文字。"""
        return " / ".join(f"{name} {len(rows)}件" for name, rows in self.rows.items() if rows)

    @classmethod
    def load(cls, path: Path) -> Self:
        if not path.exists():
            return cls()
        return cls(json.loads(path.read_text(encoding="utf-8")))

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(self.rows, ensure_ascii=False, indent=2), encoding="utf-8")
