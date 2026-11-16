import hashlib
import hmac
import ipaddress
import secrets

from fastapi import HTTPException


def digest(value: str, secret: str) -> str:
    return hmac.new(secret.encode(), value.encode(), hashlib.sha256).hexdigest()


def new_key():
    return "lab_" + secrets.token_urlsafe(32)


def fail(code: int, reason: str):
    raise HTTPException(code, detail=reason)


def client_ip(request, settings):
    peer = request.client.host if request.client else "127.0.0.1"
    try:
        addr = ipaddress.ip_address(peer)
        trusted = [ipaddress.ip_network(x.strip()) for x in settings.trusted_proxies]
        if any(addr in net for net in trusted):
            # Walk right to left, discard only explicitly trusted hops.
            chain = request.headers.get("x-forwarded-for", "").split(",")
            for hop in reversed(chain):
                if not hop.strip():
                    continue
                addr = ipaddress.ip_address(hop.strip())
                if not any(addr in net for net in trusted):
                    break
        return str(addr)
    except ValueError:
        return peer


def check_origin(request):
    origin = request.headers.get("origin")
    if origin and origin != request.app.state.settings.public_url:
        fail(403, "invalid_origin")
