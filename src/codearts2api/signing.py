"""Huawei Cloud API Gateway SDK-HMAC-SHA256 request signer.

Implemented from the official algorithm (not copied from any third-party project):

- https://support.huaweicloud.com/devg-apisign/api-sign-algorithm-001.html
- https://support.huaweicloud.com/devg-apisign/api-sign-sdk-python.html

The official algorithm is:

1. Build a CanonicalRequest from method, URI, query, headers, signed header
   names, and SHA-256 of the payload.
2. Build StringToSign = Algorithm + "\\n" + RequestDateTime + "\\n" +
   Hex(SHA-256(CanonicalRequest)).
3. signature = Hex(HMAC-SHA256(SK, StringToSign)).
4. Authorization: SDK-HMAC-SHA256 Access={AK}, SignedHeaders=..., Signature=...

Notes from the official document:

- Canonical URI path is RFC 3986-normalized. Each segment is percent-encoded.
  When *computing* the signature the URI must end with "/"; the request on the
  wire may omit that trailing slash.
- Query names and values are percent-encoded (unreserved: A-Z a-z 0-9 - _ . ~).
  Empty values still use "name=". Names are sorted by character code.
- Header names are lowercased and sorted. Header values have leading/trailing
  whitespace trimmed (internal spaces are kept). X-Sdk-Date is required.
- CanonicalHeaders already ends with a newline; the CanonicalRequest formula
  appends another newline, producing a blank line before SignedHeaders.
- If a temporary security token is present it must be sent as X-Security-Token
  and included in the signed headers.
- Some official clients attach X-Language *after* signing. Unsigned headers
  must not appear in the canonical request, or verification will fail.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Mapping
from urllib.parse import unquote, urlsplit

ALGORITHM = "SDK-HMAC-SHA256"
EMPTY_PAYLOAD_HASH = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

# Headers that are never part of the canonical request / SignedHeaders list.
DEFAULT_UNSIGNED_HEADERS = frozenset(
    {
        "authorization",
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "accept-encoding",
        "x-language",  # official clients may add this after signing
    }
)


def uri_encode(value: str) -> str:
    """Percent-encode a string per the official RFC 3986 rule (uppercase hex)."""
    # Unreserved characters stay literal; every other byte becomes %XY.
    unreserved = (
        b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.~"
    )
    out = bytearray()
    for byte in value.encode("utf-8"):
        if byte in unreserved:
            out.append(byte)
        else:
            out.extend(f"%{byte:02X}".encode("ascii"))
    return out.decode("ascii")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hmac_sha256_hex(key: str, message: str) -> str:
    return hmac.new(
        key.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def canonical_uri(path: str) -> str:
    """Normalize and encode the request path; always end with '/' for signing."""
    if not path:
        return "/"
    decoded = unquote(path)
    if not decoded.startswith("/"):
        decoded = "/" + decoded
    parts: list[str] = []
    for segment in decoded.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            if parts:
                parts.pop()
            continue
        parts.append(segment)
    encoded = "/" + "/".join(uri_encode(part) for part in parts)
    if not encoded.endswith("/"):
        encoded += "/"
    return encoded


def canonical_query_string(query: str) -> str:
    """Build the sorted, encoded query string. Empty input yields ""."""
    if not query:
        return ""
    pairs: list[tuple[str, str]] = []
    for part in query.split("&"):
        if part == "":
            continue
        if "=" in part:
            raw_name, raw_value = part.split("=", 1)
        else:
            raw_name, raw_value = part, ""
        name = uri_encode(unquote(raw_name))
        value = uri_encode(unquote(raw_value))
        pairs.append((name, value))
    pairs.sort(key=lambda item: (item[0], item[1]))
    return "&".join(f"{name}={value}" for name, value in pairs)


def _header_map(headers: Mapping[str, str]) -> dict[str, str]:
    return {str(key): str(value) for key, value in headers.items()}


def _has_header(headers: Mapping[str, str], name: str) -> bool:
    target = name.lower()
    return any(key.lower() == target for key in headers)


def _get_header(headers: Mapping[str, str], name: str) -> str | None:
    target = name.lower()
    for key, value in headers.items():
        if key.lower() == target:
            return value
    return None


def _set_header(headers: dict[str, str], name: str, value: str) -> None:
    # Replace any existing header with the same name, keeping a stable casing.
    target = name.lower()
    for key in list(headers):
        if key.lower() == target:
            del headers[key]
    headers[name] = value


def canonical_headers_and_signed(
    headers: Mapping[str, str],
    unsigned_names: set[str] | frozenset[str],
) -> tuple[str, str]:
    """Return (CanonicalHeaders, SignedHeaders).

    Each entry is ``lowercase(name) + ':' + trim(value) + '\\n'``.
    Names are sorted by character code. The last entry already carries a
    trailing newline.
    """
    collected: dict[str, str] = {}
    for key, value in headers.items():
        lower = key.lower().strip()
        if lower in unsigned_names:
            continue
        collected[lower] = value.strip()
    names = sorted(collected)
    canonical = "".join(f"{name}:{collected[name]}\n" for name in names)
    signed = ";".join(names)
    return canonical, signed


def build_canonical_request(
    method: str,
    canon_uri: str,
    canon_query: str,
    canon_headers: str,
    signed_headers: str,
    payload_hash: str,
) -> str:
    # CanonicalHeaders already ends with '\\n'; the extra join newline is the
    # blank line required by the official formula.
    return (
        f"{method.upper()}\n"
        f"{canon_uri}\n"
        f"{canon_query}\n"
        f"{canon_headers}\n"
        f"{signed_headers}\n"
        f"{payload_hash}"
    )


def build_string_to_sign(sdk_date: str, canonical_request: str) -> str:
    hashed = sha256_hex(canonical_request.encode("utf-8"))
    return f"{ALGORITHM}\n{sdk_date}\n{hashed}"


def build_authorization(ak: str, signed_headers: str, signature: str) -> str:
    # Algorithm and "Access=" are separated by a space, not a comma.
    # SignedHeaders and Signature are preceded by commas.
    return (
        f"{ALGORITHM} Access={ak}, "
        f"SignedHeaders={signed_headers}, "
        f"Signature={signature}"
    )


@dataclass(slots=True)
class SignedRequest:
    method: str
    url: str
    headers: dict[str, str]
    body: bytes
    canonical_request: str
    string_to_sign: str
    signed_headers: str
    signature: str
    authorization: str
    extra: dict[str, str] = field(default_factory=dict)


def sign_request(
    *,
    method: str,
    url: str,
    ak: str,
    sk: str,
    headers: Mapping[str, str] | None = None,
    body: bytes | str | None = None,
    security_token: str | None = None,
    project_id: str | None = None,
    sdk_date: str | None = None,
    extra_unsigned_headers: Mapping[str, str] | None = None,
    sign_language: bool = False,
) -> SignedRequest:
    """Sign an HTTP request and return headers ready to send.

    ``extra_unsigned_headers`` are attached to the outgoing request *after*
    the signature is computed (for example X-Language). They are excluded
    from the canonical request.
    """
    if body is None:
        body_bytes = b""
    elif isinstance(body, str):
        body_bytes = body.encode("utf-8")
    else:
        body_bytes = body

    parsed = urlsplit(url)
    host = parsed.netloc
    path = parsed.path or "/"
    query = parsed.query

    hdrs: dict[str, str] = _header_map(headers) if headers else {}

    if not _has_header(hdrs, "Host"):
        hdrs["Host"] = host

    if sdk_date is None:
        existing = _get_header(hdrs, "X-Sdk-Date")
        if existing:
            sdk_date = existing
        else:
            sdk_date = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            hdrs["X-Sdk-Date"] = sdk_date
    else:
        _set_header(hdrs, "X-Sdk-Date", sdk_date)

    if security_token:
        _set_header(hdrs, "X-Security-Token", security_token)
    if project_id:
        _set_header(hdrs, "X-Project-Id", project_id)

    unsigned = set(DEFAULT_UNSIGNED_HEADERS)
    if sign_language:
        unsigned.discard("x-language")
    if extra_unsigned_headers:
        unsigned.update(key.lower() for key in extra_unsigned_headers)

    payload_hash = sha256_hex(body_bytes) if body_bytes else EMPTY_PAYLOAD_HASH
    # Always hash the exact bytes (empty body included) — the constant above is
    # just the well-known SHA-256 of an empty payload.
    payload_hash = sha256_hex(body_bytes)

    canon_uri = canonical_uri(path)
    canon_query = canonical_query_string(query)
    canon_headers, signed_headers = canonical_headers_and_signed(hdrs, unsigned)
    if "x-sdk-date" not in (signed_headers.split(";") if signed_headers else []):
        raise ValueError("X-Sdk-Date must be present and included in SignedHeaders")

    canonical_request = build_canonical_request(
        method,
        canon_uri,
        canon_query,
        canon_headers,
        signed_headers,
        payload_hash,
    )
    string_to_sign = build_string_to_sign(sdk_date, canonical_request)
    signature = hmac_sha256_hex(sk, string_to_sign)
    authorization = build_authorization(ak, signed_headers, signature)

    out_headers = dict(hdrs)
    out_headers["Authorization"] = authorization
    if extra_unsigned_headers:
        for key, value in extra_unsigned_headers.items():
            out_headers[key] = value

    return SignedRequest(
        method=method.upper(),
        url=url,
        headers=out_headers,
        body=body_bytes,
        canonical_request=canonical_request,
        string_to_sign=string_to_sign,
        signed_headers=signed_headers,
        signature=signature,
        authorization=authorization,
    )
