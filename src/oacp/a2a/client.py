"""A2A JSON-RPC client.

The panel must talk to agents that were built across two A2A generations:

* 0.3 cards advertise ``url`` + ``protocolVersion`` and use the
  ``message/send`` / ``tasks/get`` / ``tasks/cancel`` methods with a ``role`` of
  ``user`` and parts tagged with ``kind``;
* newer cards advertise ``supportedInterfaces`` and use ``SendMessage`` /
  ``GetTask`` / ``CancelTask`` with proto-style field names and ``ROLE_USER``.

Rather than asking the operator to care, the client derives the dialect from the
card and, when the agent answers "method not found" or "invalid params", retries
once with the other dialect. Both answers are normalized before they leave this
module.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable

import httpx

from .. import net
from ..adp import card as cardmod

log = logging.getLogger(__name__)

METHODS: dict[str, dict[str, str]] = {
    "v03": {
        "send": "message/send",
        "get": "tasks/get",
        "cancel": "tasks/cancel",
        "pushSet": "tasks/pushNotificationConfig/set",
        "pushDel": "tasks/pushNotificationConfig/delete",
    },
    "v1": {
        "send": "SendMessage",
        "get": "GetTask",
        "cancel": "CancelTask",
        "pushSet": "CreateTaskPushNotificationConfig",
        "pushDel": "DeleteTaskPushNotificationConfig",
    },
}

#: Where a ``{url, token}`` push config rides in each dialect. Two positions
#: matter: its own RPC parameter, and the ``configuration`` object of a send.
#: The 1.0 names follow the proto fields (``TaskPushNotificationConfig`` nested
#: under ``config``; renamed to ``taskPushNotificationConfig`` once inlined).
_PUSH_PARAM_KEY: dict[str, str] = {"v03": "pushNotificationConfig", "v1": "config"}
_PUSH_INLINE_KEY: dict[str, str] = {"v03": "pushNotificationConfig", "v1": "taskPushNotificationConfig"}

_RETRYABLE_CODES = {-32601, -32602, -32700}
_RETRYABLE_STATUSES = {400, 404, 405, 415}


class A2AError(Exception):
    """A failure while speaking A2A: transport, JSON-RPC or protocol shape."""

    def __init__(
        self,
        message: str,
        *,
        code: int | str | None = None,
        status: int | None = None,
        payload: Any = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status
        self.payload = payload
        self.retryable = retryable


@dataclass(frozen=True)
class CallResult:
    """A successful JSON-RPC result plus the details worth recording."""

    result: Any
    endpoint: str
    style: str
    method: str
    request_id: str
    latency_ms: int


class A2AClient:
    """Calls agents over JSON-RPC, with the SSRF guard still switched on."""

    def __init__(
        self,
        *,
        allow_private: bool = False,
        timeout: float = 60.0,
        user_agent: str = "open-agent-control-panel",
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.allow_private = allow_private
        self.timeout = timeout
        self.user_agent = user_agent
        # Injectable for tests; when set, requests skip DNS pinning entirely.
        self.transport = transport

    # ------------------------------------------------------------------ public API

    def send_message(
        self,
        agent_card: dict[str, Any],
        text: str,
        *,
        context_id: str | None = None,
        task_id: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
        push_config: dict[str, Any] | None = None,
    ) -> CallResult:
        """Send a user message; the agent may answer with a Task or a Message.

        ``push_config`` (``{"url": ..., "token": ...}``) is offered inline in the
        same call, which is the one position that cannot race: an agent that
        finishes before our next RPC still knows where to report.

        ``attachments`` are ``{"data": <base64>, "mime": ..., "name": ...}`` dicts
        and go out as file parts. They travel *inline* rather than behind a hosted
        URL because the panel's own address is a loopback one by default: an agent
        that is not on this machine could not fetch what we published, and a
        feature that only works for agents on the same host is not a feature.
        """
        endpoint, style = self._target(agent_card)
        message_id = str(uuid.uuid4())

        def build(dialect: str) -> dict[str, Any]:
            params: dict[str, Any] = {
                "message": _message(
                    dialect, text, message_id, context_id, task_id, attachments
                )
            }
            if metadata:
                params["metadata"] = metadata
            if push_config:
                params["configuration"] = {_PUSH_INLINE_KEY[dialect]: dict(push_config)}
            return params

        return self._call_with_fallback(endpoint, style, "send", build)

    def get_task(
        self,
        agent_card: dict[str, Any],
        task_id: str,
        *,
        history_length: int | None = None,
    ) -> CallResult:
        """Fetch the current state of a task."""
        endpoint, style = self._target(agent_card)

        def build(dialect: str) -> dict[str, Any]:
            params: dict[str, Any] = {"id": task_id}
            if history_length is not None:
                params["historyLength"] = int(history_length)
            return params

        return self._call_with_fallback(endpoint, style, "get", build)

    def cancel_task(self, agent_card: dict[str, Any], task_id: str) -> CallResult:
        """Ask the agent to cancel a task."""
        endpoint, style = self._target(agent_card)

        def build(dialect: str) -> dict[str, Any]:
            return {"id": task_id}

        return self._call_with_fallback(endpoint, style, "cancel", build)

    def set_push_config(
        self,
        agent_card: dict[str, Any],
        task_id: str,
        config: dict[str, Any],
    ) -> CallResult:
        """Register a push-notification target for an existing task.

        A2A 0.3 calls this ``tasks/pushNotificationConfig/set``; the newer dialect
        calls it ``CreateTaskPushNotificationConfig``. Both take the task id and
        the ``{url, token}`` config; only the parameter name differs.
        """
        endpoint, style = self._target(agent_card)

        def build(dialect: str) -> dict[str, Any]:
            return {"taskId": task_id, _PUSH_PARAM_KEY[dialect]: dict(config)}

        return self._call_with_fallback(endpoint, style, "pushSet", build)

    def delete_push_config(
        self,
        agent_card: dict[str, Any],
        task_id: str,
        *,
        config_id: str | None = None,
    ) -> CallResult:
        """Remove a push-notification target (``.../delete`` / ``DeleteTask...``)."""
        endpoint, style = self._target(agent_card)

        def build(dialect: str) -> dict[str, Any]:
            params: dict[str, Any] = {"taskId": task_id}
            if config_id:
                params["pushNotificationConfigId"] = config_id
            return params

        return self._call_with_fallback(endpoint, style, "pushDel", build)

    # ------------------------------------------------------------------ internals

    def _target(self, agent_card: dict[str, Any]) -> tuple[str, str]:
        endpoint = cardmod.pick_endpoint(agent_card)
        if endpoint is None:
            raise A2AError("the agent card does not advertise an A2A endpoint")
        try:
            url = net.validate_endpoint(endpoint["url"], allow_private=self.allow_private)
        except net.UrlError as exc:
            raise A2AError(f"the agent endpoint is unusable: {exc}") from exc
        return url, endpoint["style"]

    def _call_with_fallback(
        self,
        endpoint: str,
        primary: str,
        action: str,
        build: Callable[[str], dict[str, Any]],
    ) -> CallResult:
        styles = [primary, "v1" if primary == "v03" else "v03"]
        last_error: A2AError | None = None
        for index, style in enumerate(styles):
            try:
                return self._rpc(endpoint, METHODS[style][action], build(style), style)
            except A2AError as exc:
                last_error = exc
                if not exc.retryable or index == len(styles) - 1:
                    raise
                log.info("retrying %s with the other A2A dialect (%s)", action, styles[1])
        assert last_error is not None
        raise last_error

    def _rpc(self, endpoint: str, method: str, params: dict[str, Any], style: str) -> CallResult:
        request_id = str(uuid.uuid4())
        body = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")

        started = time.monotonic()
        status, raw = self._post(endpoint, payload)
        elapsed = int((time.monotonic() - started) * 1000)

        try:
            envelope = json.loads(raw)
        except ValueError as exc:
            raise A2AError(
                "the agent returned a non-JSON response",
                status=status,
                payload=raw[:500].decode("utf-8", "replace"),
            ) from exc

        if not isinstance(envelope, dict):
            raise A2AError("the agent returned a JSON value that is not an object", status=status)

        error = envelope.get("error")
        if isinstance(error, dict):
            code = error.get("code")
            message = str(error.get("message") or "JSON-RPC error")
            retryable = (isinstance(code, int) and code in _RETRYABLE_CODES) or (
                status in _RETRYABLE_STATUSES
            )
            raise A2AError(
                f"{message} (code {code})" if code is not None else message,
                code=code if isinstance(code, (int, str)) else None,
                status=status,
                payload=error.get("data"),
                retryable=retryable,
            )

        if status >= 400:
            raise A2AError(
                f"the agent returned HTTP {status}",
                status=status,
                payload=envelope,
                retryable=status in _RETRYABLE_STATUSES,
            )

        if "result" not in envelope:
            raise A2AError("the agent response has neither result nor error", status=status)
        return CallResult(
            result=envelope["result"],
            endpoint=endpoint,
            style=style,
            method=method,
            request_id=request_id,
            latency_ms=elapsed,
        )

    def _post(self, endpoint: str, payload: bytes) -> tuple[int, bytes]:
        """POST the JSON-RPC envelope, pinning the address unless a test transport is set."""
        headers = {
            "content-type": "application/json",
            "accept": "application/json",
            "user-agent": self.user_agent,
        }
        url = endpoint
        extensions: dict[str, Any] = {}

        if self.transport is None:
            host, port = net.host_and_port(endpoint)
            try:
                addresses = _resolve(host, port, allow_private=self.allow_private)
            except net.BlockedAddress as exc:
                raise A2AError(f"the agent endpoint is not reachable: {exc}") from exc
            url, host_header, server_hostname = net.pin_target(endpoint, addresses[0])
            headers.update(host_header)
            extensions["sni_hostname"] = server_hostname

        timeout = httpx.Timeout(self.timeout, connect=min(10.0, self.timeout))
        try:
            with httpx.Client(
                timeout=timeout,
                verify=True,
                trust_env=False,
                follow_redirects=False,
                transport=self.transport,
            ) as client:
                request = client.build_request(
                    "POST",
                    url,
                    content=payload,
                    headers=headers,
                    extensions=extensions,
                )
                response = client.send(request)
                return response.status_code, response.content
        except httpx.TimeoutException as exc:
            raise A2AError("the agent did not answer before the timeout") from exc
        except httpx.HTTPError as exc:
            raise A2AError(f"the agent could not be reached: {exc}") from exc


def _resolve(host: str, port: int, *, allow_private: bool) -> list[str]:
    if allow_private:
        import socket

        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise net.BlockedAddress(f"DNS resolution failed for {host}: {exc}") from exc
        addresses = []
        for info in infos:
            candidate = info[4][0]
            if candidate not in addresses:
                addresses.append(candidate)
        if not addresses:
            raise net.BlockedAddress(f"DNS returned no addresses for {host}")
        return addresses
    return net.resolve_public_ips(host, port)


def _message(
    style: str,
    text: str,
    message_id: str,
    context_id: str | None,
    task_id: str | None,
    attachments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a Message in the dialect the agent speaks."""
    parts = _parts(style, text, attachments or [])
    if style == "v1":
        message: dict[str, Any] = {
            "messageId": message_id,
            "role": "ROLE_USER",
            "parts": parts,
        }
    else:
        message = {
            "messageId": message_id,
            "role": "user",
            "parts": parts,
        }
    if context_id:
        message["contextId"] = context_id
    if task_id:
        message["taskId"] = task_id
    return message


def _parts(style: str, text: str, attachments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The parts of one user message: its text first, then its files.

    A message that is only an image has no text, and an empty text part is a part
    some agents reject — so the text part is omitted rather than sent blank.

    The two dialects spell a file differently and neither spelling is optional:

    * v1    ``{"raw": <b64>, "mediaType": ..., "filename": ...}``
    * v0.3  ``{"kind": "file", "file": {"bytes": <b64>, "mimeType": ..., "name": ...}}``

    ``bytes`` and ``raw`` are the base64 of the file, not raw bytes: A2A puts them
    in a proto ``bytes`` field, which JSON transports carry as base64.
    """
    parts: list[dict[str, Any]] = []
    if text:
        parts.append({"text": text} if style == "v1" else {"kind": "text", "text": text})
    for item in attachments:
        data = str(item.get("data") or "")
        if not data:
            continue
        mime = str(item.get("mime") or "application/octet-stream")
        name = str(item.get("name") or "")
        if style == "v1":
            part: dict[str, Any] = {"raw": data, "mediaType": mime}
            if name:
                part["filename"] = name
        else:
            file: dict[str, Any] = {"bytes": data, "mimeType": mime}
            if name:
                file["name"] = name
            part = {"kind": "file", "file": file}
        parts.append(part)
    return parts
