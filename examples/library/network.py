"""
設定のディレクトリから、Networkを組み立てる。

コードに書くのは、tools.yaml の名前とツールの実装の対応だけ。それ以外
（プロンプト、モデル、スキーマ、つながり）は config/ と prompts/ にある。
"""

from pathlib import Path

from statecraft import Agent, Interceptor, LocalTools, Network, NetworkBuilder

from . import tools

ROOT = Path(__file__).resolve().parent
GUARD_ROOT = ROOT / "guard"

# tools.yaml の名前 → ツールの関数。LLM（provider: gemini）は NetworkBuilder に
# 最初から登録されている。MCP のサーバーのツールを使うなら、ここに McpTools を並べる。
LIBRARY_TOOLS = LocalTools(
    {
        "search_rules": tools.search_rules,
        "fetch_member_status": tools.fetch_member_status,
        "check_stock": tools.check_stock,
        "issue_reservation": tools.issue_reservation,
        "send_overdue_notice": tools.send_overdue_notice,
        # 全項目の見本（tools.yaml の sample_tool）
        "sample_tool": tools.sample_tool,
    }
)


def build_network(interceptor: Interceptor) -> Network:
    """
    1リクエスト分のNetworkを作る。

    Networkは1リクエストにつき1インスタンス。ここで共有記憶が1つ生成され、
    全Agentへ注入される。設定は毎回読み直すので、config/ や prompts/ を直すと
    次のリクエストから効く。
    """
    return NetworkBuilder.from_dir(ROOT, interceptor, tools=[LIBRARY_TOOLS]).build()


def build_compliance() -> Agent:
    """
    審査役を作る。図書館のネットワークとは別のNetworkに1体だけ置く。

    別のNetworkにすることで、共有記憶もinterceptorも図書館のものとは別になる
    （interceptorを渡さないので、審査中に審査が走らない）。
    """
    return NetworkBuilder.from_dir(GUARD_ROOT).build()["compliance"]
