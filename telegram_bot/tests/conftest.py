from __future__ import annotations

import boto3
import pytest
from moto import mock_aws


@pytest.fixture()
def env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token-123")
    monkeypatch.setenv("WEBHOOK_SECRET_TOKEN", "test-secret")
    monkeypatch.setenv("ALLOWED_USER_ID", "12345")
    monkeypatch.setenv("DYNAMODB_TABLE_NAME", "finance-bot")


@pytest.fixture()
def dynamodb_table(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DYNAMODB_TABLE_NAME", "finance-bot")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")

    with mock_aws():
        # Reset module-level singletons so boto3 picks up moto
        import telegram_bot.storage.dynamodb as db_mod

        db_mod._table = None
        db_mod._client = None

        resource = boto3.resource("dynamodb", region_name="us-east-1")
        table = resource.create_table(
            TableName="finance-bot",
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        table.meta.client.update_time_to_live(
            TableName="finance-bot",
            TimeToLiveSpecification={
                "Enabled": True,
                "AttributeName": "ttl",
            },
        )
        yield table

        # Reset singletons after test so next test gets fresh clients
        db_mod._table = None
        db_mod._client = None
