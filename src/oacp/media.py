"""Attachments, in both directions: accept them, check them, describe them.

Why this is a module and not three lines in ``sessions``: one attachment crosses
four layers (HTTP body → stored row → wire ``Part`` → rendered bubble) and every
layer needs the same facts about it — what type it really is, how big it really
is, and what to call it. Deciding those once, here, is what stops the layers from
disagreeing.

**The declared type is never trusted.** A caller that labels a PDF ``image/png``
is rejected by the magic bytes, not by its own claim: an agent that trusts the
content type is exactly the agent this panel would otherwise be feeding. The
declaration is only a hint about which extension and filename to keep.

Two directions, two temperaments:

* :func:`prepare` (outbound) raises. A person picked a file in a composer and is
  watching for a reason, so every rejection has a stable code the UI can say out
  loud;
* :func:`intake` (inbound) never raises. An agent's reply is not a person's
  request: a bad part must not fail the turn, and must not be silently dropped
  either. It comes back as an :class:`Intake` that either carries bytes or names
  the reason they are not being shown.

Nothing in this module parses image bytes *back*, and nothing renders them.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from dataclasses import dataclass
from typing import Any, Iterable

from .errors import PanelError

#: The image types the panel accepts. Deliberately short: every entry is a type a
#: browser renders inline *and* one the panel is willing to pass onward. SVG is
#: absent on purpose — it is a script container, not a picture.
IMAGE_TYPES: dict[str, str] = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
}

#: Spellings that mean the same type. `image/jpg` is the common one; it is a label
#: difference, not a different image, so it must not become a 415.
_MIME_ALIASES: dict[str, str] = {
    "image/jpg": "image/jpeg",
    "image/pjpeg": "image/jpeg",
    "image/x-png": "image/png",
}

#: The one type a stored text file is ever served as. A declaration of
#: ``text/html`` or ``image/svg+xml`` never reaches the database under its own
#: name: both are script containers, and this panel renders text *as text*.
TEXT_MIME = "text/plain"

#: Documents the panel keeps but never renders inside its own page. PDF is here
#: because the browser already has a reader: handing the bytes to it is safer
#: than putting a plugin boundary inside the panel's document. Outbound a person
#: cannot attach these — the composer offers pictures and text only.
DOCUMENT_TYPES: dict[str, str] = {
    "application/pdf": "pdf",
}

#: Extension per stored type. Text keeps whatever name it arrived with, so this
#: is only the fallback for a file that came without one.
EXTENSIONS: dict[str, str] = {**IMAGE_TYPES, **DOCUMENT_TYPES, TEXT_MIME: "txt"}

#: Formats that are *not* text even when their bytes decode as UTF-8. PDF is the
#: one that matters: ``%PDF-1.7`` is pure ASCII, so "does it decode?" alone would
#: store a PDF as a log and show its source in the preview. ZIP covers docx and
#: friends; OLE covers the older Office files.
BINARY_PREFIXES: tuple[bytes, ...] = (
    b"%PDF-",
    b"PK\x03\x04",
    b"PK\x05\x06",
    b"\x1f\x8b",
    b"\xd0\xcf\x11\xe0",
    b"\x7fELF",
    b"\x00asm",
)

#: How much of a file is inspected for NUL. It is the strongest single signal
#: that a file is not text, and cheap enough to run on every inbound part.
NUL_WINDOW = 64 * 1024

#: C0 controls that are *not* legal in text: everything except tab, newline,
#: form feed, carriage return and ESC. ESC stays because logs carry ANSI colour
#: codes and a browser renders them literally inside a <pre>. DEL counts as
#: binary evidence.
#:
#: Compiled, and matched at C level: :func:`sniff_text` runs on every inbound
#: part on *every poll*, before the sha256 dedupe can short-circuit, so a
#: per-character Python loop over a multi-megabyte document is a real latency
#: amplifier. The scan is not bounded to a prefix on purpose — character density
#: is a whole-file property, and a window would let a file whose second half is
#: binary pass.
_BAD_CONTROLS = re.compile(r"[\x00-\x08\x0b\x0e-\x1a\x1c-\x1f\x7f]")

#: Longest filename kept. Browsers hand over the local file name, which on some
#: systems is far longer than anything a sane agent wants to read back.
MAX_NAME = 120

#: Why an inbound file is not being shown. Stable codes, never prose: the bubble
#: that renders them is localized at paint time, and a row written in Chinese
#: would be wrong for the next reader of that conversation.
SKIP_TOO_LARGE = "too_large"
SKIP_UNSUPPORTED = "unsupported"
SKIP_BAD_DATA = "bad_data"
SKIP_UNREACHABLE = "unreachable"
SKIP_TOO_MANY = "too_many"

_UNSAFE = re.compile(r"[\x00-\x1f\x7f/\\]+")
# Parameters are allowed before ;base64 (data:image/png;charset=utf-8;base64,...).
_DATA_URL = re.compile(r"^data:[^,]*;base64,", re.IGNORECASE)
_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class Prepared:
    """One attachment, checked and ready for storage and for the wire."""

    mime: str
    name: str
    size: int
    sha256: str
    data: bytes

    @property
    def b64(self) -> str:
        """Canonical base64: re-encoded from the bytes, not from the input.

        A client may send its base64 with padding, line breaks or a data-URL
        prefix; what goes on the wire is always one padded, single-line
        encoding of exactly the bytes that were checked.
        """
        return base64.b64encode(self.data).decode("ascii")


@dataclass(frozen=True)
class Intake:
    """What an inbound file turned out to be: bytes to show, or a reason not to.

    ``identity`` is what dedupe compares. For real bytes it is the sha256 of
    those bytes; for a file that will not be shown it is a hash of *what was
    described* (type, name, reason), so a snapshot that repeats the same
    unusable part does not stack up one chip per poll.
    """

    prepared: Prepared | None
    reason: str | None
    mime: str
    name: str
    size: int
    source: str | None = None
    #: A content-derived discriminator for a chip. Two *different* inline files
    #: can share type and name (often both empty), and without this they collapse
    #: into one chip — which hides how many files the panel refused.
    digest: str | None = None

    @property
    def shown(self) -> bool:
        """True when this file has bytes the panel keeps (not a chip)."""
        return self.prepared is not None

    @property
    def sha256(self) -> str:
        """The dedupe identity of this intake (never empty)."""
        if self.prepared is not None:
            return self.prepared.sha256
        # `source` belongs in the key: two different URLs that both fail share
        # mime/name, and collapsing them would hide how many files failed.
        key = (
            f"skip:{self.reason}:{self.mime}:{self.name}:{self.source or ''}:{self.digest or ''}"
        )
        return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _hash(data: bytes) -> str:
    """sha256 of the bytes behind a chip: two unnamed files stay two chips."""
    return hashlib.sha256(data).hexdigest()


def _text_digest(text: str) -> str:
    """A chip identity for text we refuse to decode, without copying all of it.

    The pre-decode size check exists so an oversized base64 body is never
    materialised; encoding the whole string here to hash it would allocate as
    much as the ``b64decode`` the check avoids. A bounded prefix plus the length
    is enough to keep two different refused parts apart — which is all a chip
    identity is for.
    """
    return _hash(text[:4096].encode("utf-8") + str(len(text)).encode("ascii"))


def sniff(data: bytes) -> str | None:
    """Return the MIME type the bytes actually are, or None when unrecognised."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:6] in {b"GIF87a", b"GIF89a"}:
        return "image/gif"
    # WEBP is RIFF-framed: "RIFF" + 4-byte size + "WEBP".
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def sniff_document(data: bytes) -> str | None:
    """Return the type of a document kept but never rendered, or None."""
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    return None


#: ``application/*`` spellings that are really text on the wire. They are stored
#: as :data:`TEXT_MIME`; this tuple only answers "is it worth fetching at all?".
_TEXTY_APPLICATIONS: tuple[str, ...] = (
    "application/json",
    "application/xml",
    "application/javascript",
    "application/x-javascript",
    "application/x-sh",
    "application/x-yaml",
    "application/toml",
    "application/sql",
)


def accepts_document(declared: str) -> bool:
    """True when a *declared* type is one whose bytes this panel might keep.

    Used by the inbound URL path to decide whether to fetch at all: a URL from an
    agent that says ``image/png``, ``text/markdown`` or ``application/pdf`` is
    worth a guarded download, while one that says ``application/zip`` is not —
    and downloading it would only confirm the label.

    An empty declaration (or ``*/*`` / ``octet-stream``) returns True: no label
    means the bytes have to decide, which is the same rule the rest of the module
    follows.
    """
    value = str(declared or "").split(";", 1)[0].strip().lower()
    value = _MIME_ALIASES.get(value, value)
    if not value or value in {"*/*", "application/octet-stream"}:
        return True
    if value.startswith(("image/", "text/")):
        return True
    return value in DOCUMENT_TYPES or value in _TEXTY_APPLICATIONS


def sniff_text(data: bytes) -> bool:
    """True when these bytes are text this panel is willing to store and show.

    Four refusals, and every one of them is about the bytes rather than the name:

    * a known binary/document signature — a PDF is valid ASCII but is not text;
    * NUL in the first :data:`NUL_WINDOW` bytes;
    * anything that is not strict UTF-8 (UTF-16 and GBK are refused rather than
      transcoded — transcoding is changing the content);
    * more than one control character per thousand — bytes that decode but are
      full of C0 are binary that got lucky with the decoder.
    """
    if not data or data.startswith(BINARY_PREFIXES):
        return False
    if b"\x00" in data[:NUL_WINDOW]:
        return False
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return len(_BAD_CONTROLS.findall(text)) * 1000 <= len(text)


# ---------------------------------------------------------------------- outbound


def prepare(raw: Any, *, max_bytes: int) -> Prepared:
    """Check one outgoing attachment and compute what the rest of the panel needs.

    ``raw`` is the JSON shape the API accepts: ``{"data": <base64>, "mime": str,
    "name": str}``. Every rejection carries a stable code, so the UI can say what
    went wrong without matching on prose.
    """
    if not isinstance(raw, dict):
        raise PanelError("attachment_bad_data", status=400)

    # Drop parameters (`; charset=...`) and normalise aliases before comparing:
    # the declared type is a label, the sniffed type is the truth.
    declared = str(raw.get("mime") or "").split(";", 1)[0].strip().lower()
    declared = _MIME_ALIASES.get(declared, declared)
    text = raw.get("data")
    if not isinstance(text, str) or not text:
        raise PanelError("attachment_bad_data", status=400)
    # Line-wrapped base64 is legal, and Prepared.b64 promises to accept it.
    # b64decode(validate=True) rejects any whitespace, so strip it *before* both
    # the size pre-check and the decode: otherwise a valid image becomes 400.
    text = _WHITESPACE.sub("", _DATA_URL.sub("", text.strip()))

    # Size is checked on the *encoded* length before decoding: a caller who sends
    # 500 MB of base64 must not make this process allocate 375 MB to find out it
    # was too large. 3/4 is exact for normal padding, and the byte check below is
    # the authoritative one.
    if len(text) * 3 // 4 > max_bytes + 2:
        raise PanelError("attachment_too_large", status=413, max=_megabytes(max_bytes))

    try:
        data = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise PanelError("attachment_bad_data", status=400) from exc

    if not data:
        raise PanelError("attachment_bad_data", status=400)
    if len(data) > max_bytes:
        raise PanelError("attachment_too_large", status=413, max=_megabytes(max_bytes))

    mime = sniff(data)
    if mime is None:
        # Not a picture. Text is the one other thing a person may attach, and it
        # has to be proven from the bytes: a PNG renamed `notes.txt` must not
        # arrive as a log, and a PDF must not arrive as its own source.
        if not sniff_text(data):
            raise PanelError(
                "attachment_unsupported",
                status=415,
                types=", ".join(sorted(IMAGE_TYPES)) + f", {TEXT_MIME}",
            )
        if declared.startswith("image/"):
            # Labelling text as an image is a bug on the caller's side. Saying so
            # is cheaper than storing it under a type it is not.
            raise PanelError(
                "attachment_type_mismatch", status=415, declared=declared, actual=TEXT_MIME
            )
        return Prepared(
            mime=TEXT_MIME,
            name=_text_name(raw.get("name")),
            size=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            data=data,
        )

    if declared and declared != mime:
        # The declaration disagreed with the bytes. The bytes win, and the
        # mismatch is worth surfacing rather than silently correcting: a caller
        # sending `image/png` beside JPEG bytes is a bug on their side.
        raise PanelError("attachment_type_mismatch", status=415, declared=declared, actual=mime)

    return Prepared(
        mime=mime,
        name=_safe_name(raw.get("name"), mime),
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        data=data,
    )


def prepare_all(
    items: Iterable[Any] | None, *, max_bytes: int, max_count: int
) -> list[Prepared]:
    """Check every attachment of one message, enforcing the count limit first."""
    raw_items = [item for item in (items or []) if item is not None]
    if not raw_items:
        return []
    if len(raw_items) > max_count:
        raise PanelError("too_many_attachments", status=400, max=max_count)
    return [prepare(item, max_bytes=max_bytes) for item in raw_items]


# ----------------------------------------------------------------------- inbound


def intake(
    data: bytes,
    *,
    declared: str = "",
    name: str = "",
    max_bytes: int,
    source: str | None = None,
) -> Intake:
    """Decide what to do with bytes an agent sent. Never raises.

    The temper is deliberately the opposite of :func:`prepare`: an agent's part
    that is not an image, is too large, or is not even the type it claims is a
    *fact to show*, not a reason to fail the turn that carried it. The declared
    type is still ignored in favour of the bytes — but where ``prepare`` would
    reject a mismatch, ``intake`` quietly corrects it, because the alternative is
    discarding a picture over a label.
    """
    declared = str(declared or "").strip().lower()
    if not data:
        return _skip(SKIP_BAD_DATA, declared, name, 0, source)
    if len(data) > max_bytes:
        return _skip(SKIP_TOO_LARGE, declared, name, len(data), source, _hash(data))

    mime = sniff(data) or sniff_document(data)
    if mime is None:
        # Text an agent sent is kept like a picture: the whole point of storing
        # it is that a person can read it. Anything else is a chip with a reason.
        if sniff_text(data):
            shown_name = _display_name(name, TEXT_MIME)
            return Intake(
                prepared=Prepared(
                    mime=TEXT_MIME,
                    name=shown_name,
                    size=len(data),
                    sha256=hashlib.sha256(data).hexdigest(),
                    data=data,
                ),
                reason=None,
                mime=TEXT_MIME,
                name=shown_name,
                size=len(data),
                source=source,
            )
        return _skip(
            SKIP_UNSUPPORTED,
            declared or "application/octet-stream",
            name,
            len(data),
            source,
            _hash(data),
        )

    return Intake(
        prepared=Prepared(
            mime=mime,
            name=_display_name(name, mime),
            size=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            data=data,
        ),
        reason=None,
        mime=mime,
        name=_display_name(name, mime),
        size=len(data),
        source=source,
    )


def intake_b64(
    text: Any,
    *,
    declared: str = "",
    name: str = "",
    max_bytes: int,
    source: str | None = None,
) -> Intake:
    """The same decision, from the base64 an A2A ``bytes``/``raw`` field carries.

    Decoding is lenient on purpose — line breaks, a data-URL prefix and missing
    padding are all things a real agent has sent — and the size check happens on
    the decoded length, because that is the number that matters everywhere else.
    """
    if not isinstance(text, str) or not text.strip():
        return _skip(SKIP_BAD_DATA, declared, name, 0, source)
    cleaned = _WHITESPACE.sub("", _DATA_URL.sub("", text.strip()))
    # Refuse before allocating: base64 is ~4/3 of the bytes it carries.
    if len(cleaned) * 3 // 4 > max_bytes + 2:
        return _skip(
            SKIP_TOO_LARGE,
            declared,
            name,
            len(cleaned) * 3 // 4,
            source,
            _text_digest(cleaned),
        )
    cleaned += "=" * (-len(cleaned) % 4)
    try:
        data = base64.b64decode(cleaned, validate=True)
    except (binascii.Error, ValueError):
        return _skip(SKIP_BAD_DATA, declared, name, 0, source, _text_digest(cleaned))
    return intake(data, declared=declared, name=name, max_bytes=max_bytes, source=source)


def skip(
    reason: str,
    *,
    declared: str = "",
    name: str = "",
    size: int = 0,
    source: str | None = None,
    digest: str | None = None,
) -> Intake:
    """An :class:`Intake` for a file that is not shown, with a stable identity.

    ``digest`` is the identity of the bytes behind the chip, when they are known
    (an inline part the quota refused). It keeps two unrelated files from
    collapsing into a single chip just because both are unnamed PNGs.
    """
    return _skip(reason, declared, name, size, source, digest)


# ----------------------------------------------------------------------- naming


def _skip(
    reason: str,
    declared: str,
    name: str,
    size: int,
    source: str | None,
    digest: str | None = None,
) -> Intake:
    # The same normalisation the outbound path does: an agent that sends
    # `image/jpg` or `image/png; charset=utf-8` must not become `file.bin` here.
    mime = str(declared or "").split(";", 1)[0].strip().lower()
    mime = _MIME_ALIASES.get(mime, mime) or "application/octet-stream"
    return Intake(
        prepared=None,
        reason=reason,
        mime=mime,
        name=_display_name(name, mime),
        size=size,
        source=source,
        digest=digest,
    )


def name_from_url(url: Any) -> str:
    """The filename a URL path suggests, or "" when it suggests none.

    An agent that points at ``.../charts/sales.png`` has already told us what to
    call the picture, and "file.png" would throw that away. Query and fragment are
    dropped because they are not part of the name; the result still goes through
    the same sanitizer, so a path that ends in ``..`` or a control character
    cannot smuggle anything into a header or a bubble.
    """
    from urllib.parse import urlsplit

    try:
        path = urlsplit(str(url or "")).path
    except ValueError:
        return ""
    segment = path.rstrip("/").rsplit("/", 1)[-1]
    if not segment or "." not in segment:
        return ""
    return _sanitize(segment)


def _display_name(name: Any, mime: str) -> str:
    """A filename safe to store and to show, for types we may not recognise."""
    text = _sanitize(name)
    if text:
        return text
    extension = EXTENSIONS.get(mime, "bin")
    return f"file.{extension}"


def _text_name(name: Any) -> str:
    """A text file's own name, kept as it is.

    Unlike :func:`_safe_name` there is no extension to force: the bytes already
    decided it is text, and the name is the only remaining hint about *which*
    text (`.md`, `.csv`, `.log`). Rewriting it to `.txt` would throw that away.
    """
    return _sanitize(name) or f"file.{EXTENSIONS[TEXT_MIME]}"


def _safe_name(name: Any, mime: str) -> str:
    """A filename that is safe to store, to display and to put in a header.

    The extension follows the *sniffed* type, and is decided before truncation:
    ``_sanitize`` may already have cut a long name down to ``...photo.jpe``, which
    a "is it at most five characters" test would accept as a real extension.
    """
    text = _sanitize(name)
    if not text:
        return f"image.{IMAGE_TYPES[mime]}"
    wanted = IMAGE_TYPES[mime]
    stem, dot, extension = text.rpartition(".")
    if not dot or not stem:
        stem, extension = text, wanted
    elif extension.lower() != wanted:
        extension = wanted
    keep = max(1, MAX_NAME - (len(extension) + 1))
    return f"{stem[:keep]}.{extension}"


def _sanitize(name: Any) -> str:
    text = str(name or "").strip()
    # Anything that looks like a path is reduced to its last segment: the name is
    # a label, never a location.
    text = text.replace("\\", "/").rsplit("/", 1)[-1]
    text = _UNSAFE.sub("", text).strip()
    # A name made only of dots ("..", ".") is a path fragment, not a label; the
    # callers' `image.*` fallback is the honest answer. A trailing dot goes too —
    # `foo.` is not a usable filename.
    if not text.strip("."):
        return ""
    return text[:MAX_NAME].rstrip(".")


def _megabytes(value: int) -> str:
    """``5242880`` -> ``"5 MB"`` (whole numbers stay whole)."""
    size = value / (1024 * 1024)
    return f"{size:.0f} MB" if size >= 1 else f"{value} B"
