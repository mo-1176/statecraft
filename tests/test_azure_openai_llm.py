"""
AzureOpenAILLMの接続先と認証。

APIは呼ばない。openaiの代わりに偽のクライアントを差し込み、OpenAIクライアントへ
何が渡ったかを確かめる。生成の変換はOpenAILLMと同じなので、ここでは扱わない。
"""

from types import SimpleNamespace

import pytest

from statecraft import llm as llm_module
from statecraft.llm import AzureOpenAILLM

ENDPOINT = "https://example.openai.azure.com"
BASE_URL = "https://example.openai.azure.com/openai/v1/"


@pytest.fixture
def opened(monkeypatch):
    """OpenAIクライアントへ渡った引数を、作られた順に残す。"""
    calls: list[dict] = []

    def fake_client(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace()

    fake_openai = SimpleNamespace(OpenAI=fake_client, BadRequestError=Exception)
    monkeypatch.setattr(llm_module, "_require", lambda *_: fake_openai)
    # 手元の環境変数で結果が変わらないようにする
    for name in ("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    return calls


@pytest.mark.parametrize(
    "endpoint",
    [
        ENDPOINT,
        ENDPOINT + "/",
        ENDPOINT + "/openai/v1",
        ENDPOINT + "/openai/v1/",
    ],
)
def test_エンドポイントの下のv1へ繋ぐ(opened, endpoint):
    AzureOpenAILLM(endpoint=endpoint, api_key="azure-key")

    assert opened == [{"api_key": "azure-key", "base_url": BASE_URL}]


def test_渡さなければ環境変数のエンドポイントとキーを使う(opened, monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", ENDPOINT)
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "azure-key")

    AzureOpenAILLM()

    assert opened == [{"api_key": "azure-key", "base_url": BASE_URL}]


def test_token_providerは呼ばずに関数のまま渡す(opened):
    """SDKがリクエストごとに呼ぶ。ここで呼んで文字列にすると、期限が切れた後も同じトークンを送る。"""

    def token_provider() -> str:
        raise AssertionError("組み立て時に呼んではならない")

    AzureOpenAILLM(endpoint=ENDPOINT, token_provider=token_provider)

    assert opened[0]["api_key"] is token_provider


def test_token_providerを渡せば環境変数のキーは使わない(opened, monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "azure-key")

    def token_provider() -> str:
        return "token"

    AzureOpenAILLM(endpoint=ENDPOINT, token_provider=token_provider)

    assert opened[0]["api_key"] is token_provider


def test_api_keyとtoken_providerは同時に渡せない(opened):
    with pytest.raises(ValueError, match="どちらか1つ"):
        AzureOpenAILLM(endpoint=ENDPOINT, api_key="azure-key", token_provider=lambda: "token")

    assert opened == []


def test_OpenAIのキーをAzureへ送らない(opened, monkeypatch):
    """Azureの認証情報が無いままSDKへ渡すと、SDKはOPENAI_API_KEYを読んで送ってしまう。"""
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")

    with pytest.raises(ValueError, match="AZURE_OPENAI_API_KEY"):
        AzureOpenAILLM(endpoint=ENDPOINT)

    assert opened == []


def test_エンドポイントが無ければOpenAIへ繋がない(opened, monkeypatch):
    """接続先が無いままSDKへ渡すと、SDKはapi.openai.comへAzureのキーを送ってしまう。"""
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "azure-key")

    with pytest.raises(ValueError, match="AZURE_OPENAI_ENDPOINT"):
        AzureOpenAILLM()

    assert opened == []
