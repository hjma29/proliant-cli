"""Tests for 'proliant com whoami' and the email/login_method tracking it relies on.

Covers:
  - login.save_token() persists 'email' and 'login_method', and preserves both
    across a token refresh that doesn't pass them (see refresh_token_if_needed()).
  - cli._cmd_whoami() reports the right identity for each credential source:
    env vars, credentials.yml (API client), and token.json (user login), and
    exits(1) with a clear message when nothing is configured.
"""
from __future__ import annotations

import argparse
import json

import pytest

from proliant.com import login as login_mod


class TestSaveTokenPersistsIdentity:
    def test_saves_email_and_login_method(self, tmp_path, monkeypatch):
        token_path = tmp_path / "token.json"
        monkeypatch.setattr(login_mod, "TOKEN_CACHE", token_path)

        login_mod.save_token(
            {"access_token": "a", "refresh_token": "r", "id_token": "i", "expires_in": 3600},
            region="us-west", workspace_id="ws-1", workspace_name="my-ws",
            email="user@example.com", login_method="password",
        )

        saved = json.loads(token_path.read_text())
        assert saved["email"] == "user@example.com"
        assert saved["login_method"] == "password"

    def test_refresh_without_identity_args_preserves_existing(self, tmp_path, monkeypatch):
        """refresh_token_if_needed() calls save_token() without email/login_method --
        those fields must survive instead of being blanked out."""
        token_path = tmp_path / "token.json"
        monkeypatch.setattr(login_mod, "TOKEN_CACHE", token_path)
        token_path.write_text(json.dumps({
            "email": "user@example.com", "login_method": "okta_verify",
            "workspace_regions": {},
        }))

        login_mod.save_token(
            {"access_token": "a2", "refresh_token": "r2", "id_token": "i2", "expires_in": 3600},
            region="us-west", workspace_id="ws-1", workspace_name="my-ws",
        )

        saved = json.loads(token_path.read_text())
        assert saved["email"] == "user@example.com"
        assert saved["login_method"] == "okta_verify"


class TestCmdWhoami:
    @pytest.mark.asyncio
    async def test_not_logged_in_exits_1(self, tmp_path, monkeypatch, capsys):
        from proliant.com import cli

        monkeypatch.delenv("HPECOM_CLIENT_ID", raising=False)
        monkeypatch.delenv("HPECOM_CLIENT_SECRET", raising=False)
        monkeypatch.setattr(login_mod, "CREDS_FILE", tmp_path / "credentials.yml")
        monkeypatch.setattr(login_mod, "TOKEN_CACHE", tmp_path / "token.json")

        with pytest.raises(SystemExit) as exc_info:
            await cli._cmd_whoami(argparse.Namespace())

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "Not logged in" in captured.out

    @pytest.mark.asyncio
    async def test_env_var_credentials_reported(self, tmp_path, monkeypatch, capsys):
        from proliant.com import cli

        monkeypatch.setenv("HPECOM_CLIENT_ID", "env-client-id")
        monkeypatch.setenv("HPECOM_CLIENT_SECRET", "env-secret")
        monkeypatch.setattr(login_mod, "CREDS_FILE", tmp_path / "credentials.yml")
        monkeypatch.setattr(login_mod, "TOKEN_CACHE", tmp_path / "token.json")

        await cli._cmd_whoami(argparse.Namespace())

        captured = capsys.readouterr()
        assert "env-client-id" in captured.out
        assert "env vars" in captured.out.lower()

    @pytest.mark.asyncio
    async def test_api_client_credentials_file_reported(self, tmp_path, monkeypatch, capsys):
        from proliant.com import cli

        monkeypatch.delenv("HPECOM_CLIENT_ID", raising=False)
        monkeypatch.delenv("HPECOM_CLIENT_SECRET", raising=False)
        creds_path = tmp_path / "credentials.yml"
        creds_path.write_text("client_id: file-client-id\nclient_secret: shh\nregion: eu-central\n")
        monkeypatch.setattr(login_mod, "CREDS_FILE", creds_path)
        monkeypatch.setattr(login_mod, "TOKEN_CACHE", tmp_path / "token.json")

        await cli._cmd_whoami(argparse.Namespace())

        captured = capsys.readouterr()
        assert "file-client-id" in captured.out

    @pytest.mark.asyncio
    async def test_user_token_reports_email_and_login_method(self, tmp_path, monkeypatch, capsys):
        from proliant.com import cli

        monkeypatch.delenv("HPECOM_CLIENT_ID", raising=False)
        monkeypatch.delenv("HPECOM_CLIENT_SECRET", raising=False)
        monkeypatch.setattr(login_mod, "CREDS_FILE", tmp_path / "credentials.yml")
        token_path = tmp_path / "token.json"
        token_path.write_text(json.dumps({
            "email": "someone@gmail.com",
            "login_method": "password",
            "workspace_name": "My Workspace",
            "region": "us-west",
        }))
        monkeypatch.setattr(login_mod, "TOKEN_CACHE", token_path)

        await cli._cmd_whoami(argparse.Namespace())

        captured = capsys.readouterr()
        assert "someone@gmail.com" in captured.out
        assert "Username + password" in captured.out
        assert "My Workspace" in captured.out

    @pytest.mark.asyncio
    async def test_user_token_missing_identity_shows_unknown_not_crash(self, tmp_path, monkeypatch, capsys):
        """Tokens saved before this feature existed have no email/login_method --
        whoami must degrade gracefully instead of erroring."""
        from proliant.com import cli

        monkeypatch.delenv("HPECOM_CLIENT_ID", raising=False)
        monkeypatch.delenv("HPECOM_CLIENT_SECRET", raising=False)
        monkeypatch.setattr(login_mod, "CREDS_FILE", tmp_path / "credentials.yml")
        token_path = tmp_path / "token.json"
        token_path.write_text(json.dumps({"workspace_name": "My Workspace", "region": "us-west"}))
        monkeypatch.setattr(login_mod, "TOKEN_CACHE", token_path)

        await cli._cmd_whoami(argparse.Namespace())

        captured = capsys.readouterr()
        assert "unknown" in captured.out.lower()
