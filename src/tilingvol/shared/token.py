"""Result tokens: an HMAC over the server's job nonce, a fresh client salt and every field of the result.
The server accepts each salt and each job nonce only once, so a token can never be replayed or moved to
another result. The key ships inside the client, so this only raises the bar; it is not a proof."""

import hashlib
import hmac
import json
import secrets

SALT_BYTES = 16


def new_salt() -> str:
    return secrets.token_hex(SALT_BYTES)


def solution_digest(solution: list[list[int]] | None) -> str:
    if solution is None:
        return ""
    return hashlib.sha256(
        json.dumps(solution, separators=(",", ":")).encode()
    ).hexdigest()


def signing_payload(
    job_id: str,
    nonce: str,
    name: str,
    width: int,
    height: int,
    verdict: str,
    stage_index: int,
    duration_s: float,
    salt: str,
    solution: list[list[int]] | None,
) -> bytes:
    fields = {
        "job_id": job_id,
        "nonce": nonce,
        "name": name,
        "width": width,
        "height": height,
        "verdict": verdict,
        "stage_index": stage_index,
        "duration_s": f"{duration_s:.1f}",
        "salt": salt,
        "solution": solution_digest(solution),
    }
    return json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()


def sign(key: bytes, payload: bytes) -> str:
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def check(key: bytes, payload: bytes, token: str) -> bool:
    return hmac.compare_digest(sign(key, payload), token)
