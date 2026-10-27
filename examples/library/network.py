"""
設定のディレクトリから、Networkを組み立てる。

コードに書くのは、tools.yaml の type とツールの実装の対応だけ。それ以外
（プロンプト、モデル、スキーマ、つながり）は config/ と prompts/ にある。
"""

from collections.abc import Callable
from pathlib import Path
from typing import ClassVar

from statecraft import Agent, Interceptor, Network, NetworkBuilder

from . import tools

ROOT = Path(__file__).resolve().parent
GUARD_ROOT = ROOT / "guard"


class LibraryBuilder(NetworkBuilder):
    # type → ツールの関数を返すもの。settings を受け取る場合は、キーワード引数で渡る。
    # 図書館のツールは settings を持たないので、関数をそのまま返す lambda で足りる。
    # LLM（provider: gemini）は NetworkBuilder に最初から登録されている。
    TOOL_FACTORIES: ClassVar[dict[str, Callable[..., Callable]]] = {
        "search_rules": lambda: tools.search_rules,
        "fetch_member_status": lambda: tools.fetch_member_status,
        "check_stock": lambda: tools.check_stock,
        "issue_reservation": lambda: tools.issue_reservation,
        "send_overdue_notice": lambda: tools.send_overdue_notice,
        # 全項目の見本（tools.yaml の sample_tool）。settings を受け取って関数を返すものは、
        # lambda で包まずにそのまま登録する
        "sample_tool": tools.make_sample_tool,
    }


def build_network(interceptor: Interceptor) -> Network:
    """
    1リクエスト分のNetworkを作る。

    Networkは1リクエストにつき1インスタンス。ここで共有記憶が1つ生成され、
    全Agentへ注入される。設定は毎回読み直すので、config/ や prompts/ を直すと
    次のリクエストから効く。
    """
    return LibraryBuilder.from_dir(ROOT, interceptor).build()


def build_compliance() -> Agent:
    """
    審査役を作る。図書館のネットワークとは別のNetworkに1体だけ置く。

    別のNetworkにすることで、共有記憶もinterceptorも図書館のものとは別になる
    （interceptorを渡さないので、審査中に審査が走らない）。
    """
    return NetworkBuilder.from_dir(GUARD_ROOT).build()["compliance"]
