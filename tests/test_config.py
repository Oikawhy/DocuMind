"""Configuration security-contract tests."""

from pathlib import Path

import pytest

from documind.config import Settings


def test_env_example_covers_every_settings_field() -> None:
    """The example environment documents every application setting."""
    env_example = Path(__file__).resolve().parents[1] / ".env.example"
    documented_keys = {
        line.partition("=")[0].strip()
        for line in env_example.read_text().splitlines()
        if line.strip().startswith("DOCUMIND_") and "=" in line
    }
    expected_keys = {f"DOCUMIND_{field_name.upper()}" for field_name in Settings.model_fields}

    assert expected_keys <= documented_keys


@pytest.mark.parametrize(
    ("layers", "expected_database_url_ref"),
    [
        pytest.param({}, "", id="release-default"),
        pytest.param(
            {"signed_profile_values": {"database_url_ref": "openbao://secret/data/documind/signed-profile#url"}},
            "openbao://secret/data/documind/signed-profile#url",
            id="signed-profile",
        ),
        pytest.param(
            {
                "signed_profile_values": {"database_url_ref": "openbao://secret/data/documind/signed-profile#url"},
                "customer_values": {"database_url_ref": "openbao://secret/data/documind/customer#url"},
            },
            "openbao://secret/data/documind/customer#url",
            id="customer",
        ),
        pytest.param(
            {
                "signed_profile_values": {"database_url_ref": "openbao://secret/data/documind/signed-profile#url"},
                "customer_values": {"database_url_ref": "openbao://secret/data/documind/customer#url"},
                "openbao_references": {"database_url_ref": "openbao://secret/data/documind/openbao#url"},
            },
            "openbao://secret/data/documind/openbao#url",
            id="openbao-reference",
        ),
        pytest.param(
            {
                "signed_profile_values": {"database_url_ref": "openbao://secret/data/documind/signed-profile#url"},
                "customer_values": {"database_url_ref": "openbao://secret/data/documind/customer#url"},
                "openbao_references": {"database_url_ref": "openbao://secret/data/documind/openbao#url"},
                "emergency_overrides": {"database_url_ref": "openbao://secret/data/documind/emergency#url"},
            },
            "openbao://secret/data/documind/emergency#url",
            id="emergency",
        ),
    ],
)
def test_settings_apply_documented_precedence(
    layers: dict[str, dict[str, str]], expected_database_url_ref: str
) -> None:
    """Later configuration layers override earlier layers in documented order."""
    settings = Settings.from_precedence_layers(**layers)

    assert settings.database_url_ref == expected_database_url_ref


@pytest.mark.parametrize(
    "openbao_references",
    [
        pytest.param(
            {"database_url": "openbao://secret/data/documind/database#url"},
            id="non-reference-field",
        ),
        pytest.param(
            {"database_url_ref": "postgresql://documind:password@database/documind"},
            id="raw-credential-url",
        ),
        pytest.param(
            {"unknown_ref": "openbao://secret/data/documind/unknown#value"},
            id="unknown-reference-field",
        ),
    ],
)
def test_openbao_layer_rejects_non_reference_values(
    openbao_references: dict[str, str],
) -> None:
    """The OpenBao layer accepts only known reference fields and reference URIs."""
    with pytest.raises(ValueError, match="OpenBao reference layer"):
        Settings.from_precedence_layers(openbao_references=openbao_references)


def test_secret_bearing_backend_urls_are_openbao_references(monkeypatch: object) -> None:
    """Redis and database credentials are represented only by OpenBao references."""
    monkeypatch.setenv("DOCUMIND_DATABASE_URL_REF", "openbao://secret/data/documind/database#url")
    monkeypatch.setenv("DOCUMIND_REDIS_URL_REF", "openbao://secret/data/documind/redis#url")
    monkeypatch.setenv("DOCUMIND_REDIS_STREAMS_URL_REF", "openbao://secret/data/documind/redis#streams_url")

    settings = Settings(_env_file=None)

    assert settings.database_url_ref == "openbao://secret/data/documind/database#url"
    assert settings.redis_url_ref == "openbao://secret/data/documind/redis#url"
    assert settings.redis_streams_url_ref == "openbao://secret/data/documind/redis#streams_url"
    assert "redis_url" not in Settings.model_fields
    assert "redis_streams_url" not in Settings.model_fields
