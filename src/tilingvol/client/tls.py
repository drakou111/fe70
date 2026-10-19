"""HTTPS for the app. Certificates are checked against certifi's current root list first: some Windows
machines keep expired cross-signed ISRG certificates in their intermediate store, and OpenSSL can build
the chain through one of those ("certificate has expired") although the server's chain is valid. The
system store stays as the second try, for networks that add their own root. Both verify fully."""

import ssl
import urllib.error
import urllib.request

import certifi

BUNDLED_CONTEXT = ssl.create_default_context(cafile=certifi.where())
SYSTEM_CONTEXT = ssl.create_default_context()


def urlopen(request: urllib.request.Request, timeout: float):
    try:
        return urllib.request.urlopen(request, timeout=timeout, context=BUNDLED_CONTEXT)
    except urllib.error.URLError as error:
        if not isinstance(error.reason, ssl.SSLCertVerificationError):
            raise
        return urllib.request.urlopen(request, timeout=timeout, context=SYSTEM_CONTEXT)
