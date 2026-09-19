"""Talking to the services that actually carry a message.

Every provider returns a `Result` with one of three outcomes, and the distinction between them is
the whole reason this module is separate from the dispatcher:

* `sent`         — acknowledged, with an id where the provider gives one
* `failed`       — refused, clearly, and safe to try again
* `ambiguous`    — we do not know. A timeout, a dropped connection, a 5xx *after* the request was
                   accepted, a 2xx with no id. The message may have gone out.

The temptation is to treat the third as the second, because retrying is easy and the failure path is
already written. That is exactly how a board receives the same report twice, so an ambiguous result
is never retried here: it is handed back for a person to resolve.

Credentials are read from the environment at call time and never stored, logged or echoed into an
error message. A missing credential is a configuration problem, reported as `failed` with a message
naming the variable, not the value.
"""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from ..util.log import get_logger

log = get_logger("delivery.providers")

# Sending had started when these happened, so whether the message went out is unknown.
AMBIGUOUS_ERRORS = (TimeoutError, ConnectionResetError, ConnectionAbortedError, BrokenPipeError)


class Result:
    def __init__(self, outcome: str, *, message_id: str | None = None, error: str | None = None,
                 status: int | None = None, response: str | None = None):
        assert outcome in ("sent", "failed", "ambiguous")
        self.outcome = outcome
        self.message_id = message_id
        self.error = error
        self.status = status
        self.response = response

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Result {self.outcome} status={self.status} id={self.message_id} error={self.error!r}>"


def _env(name: str | None) -> str | None:
    return os.environ.get(name) if name else None


def _post(url: str, *, data: bytes, headers: dict[str, str], timeout: int) -> tuple[int, str]:
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")


class MicrosoftGraph:
    """Email through Microsoft Graph, client-credentials flow, application permission Mail.Send.

    Graph answers a successful `sendMail` with 202 Accepted and an empty body — no message id. That
    is a confirmed send, not an ambiguous one: the request was accepted for delivery, which is all
    this layer can ever know. A 5xx or a timeout, by contrast, leaves the question open.
    """

    name = "microsoft_graph"

    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        graph = cfg.get("microsoft_graph") or {}
        self.tenant = _env(graph.get("tenant_id_env"))
        self.client_id = _env(graph.get("client_id_env"))
        self.client_secret = _env(graph.get("client_secret_env"))
        self.sender = _env(cfg.get("sender_env"))
        self.authority = graph.get("authority", "https://login.microsoftonline.com")
        self.endpoint = graph.get("endpoint", "https://graph.microsoft.com/v1.0").rstrip("/")
        self.scope = graph.get("scope", "https://graph.microsoft.com/.default")
        self.timeout = int(graph.get("timeout_seconds", 60))
        self.max_attachment_mb = float(graph.get("max_attachment_mb", 3.5))
        self._token: tuple[str, float] | None = None

    def missing_credentials(self) -> list[str]:
        graph = self.cfg.get("microsoft_graph") or {}
        wanted = {graph.get("tenant_id_env"): self.tenant, graph.get("client_id_env"): self.client_id,
                  graph.get("client_secret_env"): self.client_secret, self.cfg.get("sender_env"): self.sender}
        return sorted(name for name, value in wanted.items() if name and not value)

    def _access_token(self) -> str:
        if self._token and self._token[1] > time.time() + 60:
            return self._token[0]
        body = urllib.parse.urlencode({"client_id": self.client_id, "client_secret": self.client_secret,
                                       "scope": self.scope, "grant_type": "client_credentials"}).encode()
        status, text = _post(f"{self.authority}/{self.tenant}/oauth2/v2.0/token", data=body,
                             headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=self.timeout)
        payload = json.loads(text)
        token = payload["access_token"]
        self._token = (token, time.time() + int(payload.get("expires_in", 3600)))
        return token

    def send(self, *, to: str, subject: str, body_text: str, body_html: str,
             attachment: Path | None = None) -> Result:
        missing = self.missing_credentials()
        if missing:
            return Result("failed", error=f"missing credentials in the environment: {', '.join(missing)}")

        message: dict[str, Any] = {
            "subject": subject,
            "body": {"contentType": "HTML", "content": body_html},
            "toRecipients": [{"emailAddress": {"address": to}}],
        }
        note = None
        if attachment and attachment.exists():
            size_mb = attachment.stat().st_size / (1024 * 1024)
            if size_mb <= self.max_attachment_mb:
                message["attachments"] = [{
                    "@odata.type": "#microsoft.graph.fileAttachment",
                    "name": attachment.name,
                    "contentType": "application/pdf",
                    "contentBytes": base64.b64encode(attachment.read_bytes()).decode("ascii"),
                }]
            else:
                # Too large to attach this way. The message still goes, saying so, rather than
                # failing and leaving the recipient with nothing.
                note = (f"[{attachment.name} is {size_mb:.1f} MB, over the {self.max_attachment_mb} MB limit for "
                        f"an attached message; open it from the archive instead]")
                message["body"]["content"] = body_html + f'<p style="color:#8a6d00;font-size:12px">{note}</p>'

        data = json.dumps({"message": message, "saveToSentItems": True}).encode("utf-8")
        url = f"{self.endpoint}/users/{urllib.parse.quote(self.sender)}/sendMail"
        try:
            token = self._access_token()
        except urllib.error.HTTPError as exc:
            return Result("failed", status=exc.code, error=f"token request rejected: HTTP {exc.code}",
                          response=_body(exc))
        except AMBIGUOUS_ERRORS + (urllib.error.URLError, OSError) as exc:
            # nothing was sent: the token call happens before any message leaves
            return Result("failed", error=f"token request failed: {type(exc).__name__}: {exc}")

        try:
            status, text = _post(url, data=data,
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                                 timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            if exc.code >= 500:
                return Result("ambiguous", status=exc.code, response=_body(exc),
                              error=f"Graph returned HTTP {exc.code} after accepting the request; whether the "
                                    f"message was queued is unknown")
            return Result("failed", status=exc.code, response=_body(exc), error=f"Graph rejected the message: HTTP {exc.code}")
        except AMBIGUOUS_ERRORS as exc:
            return Result("ambiguous", error=f"connection lost while sending: {type(exc).__name__}: {exc}")
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, AMBIGUOUS_ERRORS):
                return Result("ambiguous", error=f"connection lost while sending: {reason}")
            return Result("failed", error=f"could not reach Graph: {reason}")
        except OSError as exc:
            return Result("ambiguous", error=f"connection failed mid-send: {type(exc).__name__}: {exc}")

        if status in (200, 202):
            # Graph gives no id for sendMail; acceptance is the acknowledgement.
            return Result("sent", message_id=f"graph-accepted-{status}", status=status,
                          response=(note or text or "")[:500])
        return Result("ambiguous", status=status, response=text[:500],
                      error=f"unexpected status {status} from Graph; whether the message was queued is unknown")


class MetaWhatsApp:
    """WhatsApp notifications through the official Cloud API, using approved templates only.

    A template message is the only thing sent: free-form text outside a customer service window is
    rejected by the provider, and a notification that a report is ready does not need to carry the
    report. Nothing numeric is sent over this channel.
    """

    name = "meta_cloud_api"

    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        meta = cfg.get("meta_cloud_api") or {}
        self.phone_number_id = _env(meta.get("phone_number_id_env"))
        self.token = _env(meta.get("access_token_env"))
        self.endpoint = meta.get("endpoint", "https://graph.facebook.com/v21.0").rstrip("/")
        self.timeout = int(meta.get("timeout_seconds", 30))
        self.templates = cfg.get("templates") or {}

    def missing_credentials(self) -> list[str]:
        meta = self.cfg.get("meta_cloud_api") or {}
        wanted = {meta.get("phone_number_id_env"): self.phone_number_id, meta.get("access_token_env"): self.token}
        return sorted(name for name, value in wanted.items() if name and not value)

    def send_template(self, *, to: str, template_key: str, variables: dict[str, str]) -> Result:
        missing = self.missing_credentials()
        if missing:
            return Result("failed", error=f"missing credentials in the environment: {', '.join(missing)}")
        spec = self.templates.get(template_key)
        if not spec:
            return Result("failed", error=f"no template configured under {template_key!r}")
        # Order matters: WhatsApp templates take positional variables, and a mismatch between the
        # order here and the order approved with the provider sends the wrong words to a person.
        ordered = [str(variables.get(name, "")) for name in (spec.get("variables") or [])]
        missing_vars = [n for n in (spec.get("variables") or []) if not variables.get(n)]
        if missing_vars:
            return Result("failed", error=f"template {spec['name']!r} needs {', '.join(missing_vars)}")

        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "template",
            "template": {
                "name": spec["name"],
                "language": {"code": spec.get("language", "en")},
                "components": [{"type": "body",
                                "parameters": [{"type": "text", "text": v} for v in ordered]}],
            },
        }
        url = f"{self.endpoint}/{self.phone_number_id}/messages"
        try:
            status, text = _post(url, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Authorization": f"Bearer {self.token}",
                                          "Content-Type": "application/json"}, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            if exc.code >= 500:
                return Result("ambiguous", status=exc.code, response=_body(exc),
                              error=f"provider returned HTTP {exc.code} after accepting the request")
            return Result("failed", status=exc.code, response=_body(exc),
                          error=f"provider rejected the message: HTTP {exc.code}")
        except AMBIGUOUS_ERRORS as exc:
            return Result("ambiguous", error=f"connection lost while sending: {type(exc).__name__}: {exc}")
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, AMBIGUOUS_ERRORS):
                return Result("ambiguous", error=f"connection lost while sending: {reason}")
            return Result("failed", error=f"could not reach the provider: {reason}")
        except OSError as exc:
            return Result("ambiguous", error=f"connection failed mid-send: {type(exc).__name__}: {exc}")

        if status == 200:
            try:
                mid = (json.loads(text).get("messages") or [{}])[0].get("id")
            except (ValueError, IndexError, AttributeError):
                mid = None
            if mid:
                return Result("sent", message_id=mid, status=status, response=text[:500])
            # 200 without an id: the provider accepted something, but not identifiably this message
            return Result("ambiguous", status=status, response=text[:500],
                          error="the provider returned success without a message id")
        return Result("ambiguous", status=status, response=text[:500],
                      error=f"unexpected status {status}; whether the message was sent is unknown")


class TwilioWhatsApp:
    """The alternative official route, for a deployment that already has a Twilio account."""

    name = "twilio"

    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        tw = cfg.get("twilio") or {}
        self.account_sid = _env(tw.get("account_sid_env"))
        self.auth_token = _env(tw.get("auth_token_env"))
        self.sender = _env(tw.get("from_env"))
        self.endpoint = tw.get("endpoint", "https://api.twilio.com/2010-04-01").rstrip("/")
        self.timeout = int(tw.get("timeout_seconds", 30))
        self.templates = cfg.get("templates") or {}

    def missing_credentials(self) -> list[str]:
        tw = self.cfg.get("twilio") or {}
        wanted = {tw.get("account_sid_env"): self.account_sid, tw.get("auth_token_env"): self.auth_token,
                  tw.get("from_env"): self.sender}
        return sorted(name for name, value in wanted.items() if name and not value)

    def send_template(self, *, to: str, template_key: str, variables: dict[str, str]) -> Result:
        missing = self.missing_credentials()
        if missing:
            return Result("failed", error=f"missing credentials in the environment: {', '.join(missing)}")
        spec = self.templates.get(template_key) or {}
        ordered = {str(i + 1): str(variables.get(name, "")) for i, name in enumerate(spec.get("variables") or [])}
        body = urllib.parse.urlencode({
            "To": f"whatsapp:{to}", "From": f"whatsapp:{self.sender}",
            "ContentSid": spec.get("content_sid", spec.get("name", "")),
            "ContentVariables": json.dumps(ordered),
        }).encode()
        auth = base64.b64encode(f"{self.account_sid}:{self.auth_token}".encode()).decode()
        url = f"{self.endpoint}/Accounts/{self.account_sid}/Messages.json"
        try:
            status, text = _post(url, data=body, headers={"Authorization": f"Basic {auth}",
                                                          "Content-Type": "application/x-www-form-urlencoded"},
                                 timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            if exc.code >= 500:
                return Result("ambiguous", status=exc.code, response=_body(exc),
                              error=f"provider returned HTTP {exc.code} after accepting the request")
            return Result("failed", status=exc.code, response=_body(exc),
                          error=f"provider rejected the message: HTTP {exc.code}")
        except AMBIGUOUS_ERRORS as exc:
            return Result("ambiguous", error=f"connection lost while sending: {type(exc).__name__}: {exc}")
        except (urllib.error.URLError, OSError) as exc:
            return Result("ambiguous", error=f"connection failed mid-send: {type(exc).__name__}: {exc}")
        if status in (200, 201):
            try:
                mid = json.loads(text).get("sid")
            except ValueError:
                mid = None
            return (Result("sent", message_id=mid, status=status, response=text[:500]) if mid
                    else Result("ambiguous", status=status, response=text[:500],
                                error="the provider returned success without a message sid"))
        return Result("ambiguous", status=status, response=text[:500], error=f"unexpected status {status}")


class NullProvider:
    """What runs when delivery is off, or in a dry run: composes everything, sends nothing.

    This is what makes the whole path exercisable before an account exists. It records exactly what
    would have gone out, and is never mistaken for a real send: its results are marked `skipped` by
    the dispatcher, not `sent`.
    """

    name = "none"

    def __init__(self, reason: str = "delivery is disabled"):
        self.reason = reason

    def missing_credentials(self) -> list[str]:
        return []

    def send(self, **kwargs: Any) -> Result:
        log.info("not sending (%s): %s", self.reason, kwargs.get("subject"))
        return Result("failed", error=self.reason)

    def send_template(self, **kwargs: Any) -> Result:
        log.info("not sending (%s): template %s", self.reason, kwargs.get("template_key"))
        return Result("failed", error=self.reason)


def _body(exc: urllib.error.HTTPError) -> str:
    try:
        return exc.read().decode("utf-8", "replace")[:500]
    except Exception:  # pragma: no cover - the error body is best-effort
        return ""


def email_provider(cfg: dict[str, Any]):
    provider = (cfg or {}).get("provider", "microsoft_graph")
    if provider == "microsoft_graph":
        return MicrosoftGraph(cfg)
    raise ValueError(f"unknown email provider {provider!r}; configured providers: microsoft_graph")


def whatsapp_provider(cfg: dict[str, Any]):
    provider = (cfg or {}).get("provider", "meta_cloud_api")
    if provider == "meta_cloud_api":
        return MetaWhatsApp(cfg)
    if provider == "twilio":
        return TwilioWhatsApp(cfg)
    raise ValueError(f"unknown WhatsApp provider {provider!r}; configured providers: meta_cloud_api, twilio")
