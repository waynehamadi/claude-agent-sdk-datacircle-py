"""One HTTPS GET to Datacircle's open API (https://api.datacircle.dev/v1), no key, and what its answers are cut to for a model."""

import json
import re
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Mapping, Optional, Tuple

import anyio
import anyio.to_thread

BASE_URL = "https://api.datacircle.dev/v1"
VERSION = "0.1.0"
USER_AGENT = f"claude-agent-sdk-datacircle/{VERSION}"
TIMEOUT = 30.0

# A plan holds about 90 keys (its terms of use, its aggregators...): the tools keep the ones that choose a plan, the rest is on its page.
PLAN_KEYS = ["api", "rank", "provider", "plan", "score", "perThousand", "notFoundPerThousand", "billing", "pay",
             "currency", "units", "unit", "strengths", "latencyMs", "page", "pricingPage", "docs"]
API_KEYS = ["api", "slug", "plans", "providers", "best", "bestPage", "sheetPage"]


class DatacircleError(Exception):
    """An answer of 4xx in the API's own words, or why the API could not be reached. status is the API's HTTP status,
    None when it gave none."""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


def brief(item: Mapping[str, Any], keys: List[str] = PLAN_KEYS) -> Dict[str, Any]:
    return {key: item[key] for key in keys if item.get(key) is not None}


def objects(value: Any) -> List[Dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def slug(value: Any) -> str:
    """The API's rule for a slug: the name in lowercase letters and digits, the rest a hyphen."""
    plain = unicodedata.normalize("NFKD", str(value or "").strip().lower())
    plain = "".join(char for char in plain if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", "-", plain).strip("-")


def _words(status: int, reason: str, body: bytes) -> str:
    """A refusal's words: the API's error, or its status when it gave no JSON."""
    try:
        error = json.loads(body).get("error")
    except (ValueError, AttributeError):
        error = None
    return error if isinstance(error, str) and error else f"{status} {reason}".strip()


class Client:
    """Where the tools call: base_url, https://api.datacircle.dev/v1 when left out, waiting timeout seconds at most
    for an answer. A call runs urllib in a worker thread, so the tools work in any event loop, asyncio's or trio's."""

    def __init__(self, base_url: Optional[str] = None, timeout: Optional[float] = None):
        self.base_url = (base_url or BASE_URL).rstrip("/")
        self.timeout = TIMEOUT if timeout is None else timeout

    async def get(self, path: str, params: Optional[Mapping[str, Any]] = None) -> Any:
        """The API's JSON answer, or a DatacircleError with its words: waits once when it answers 429. A run that is
        stopped stops waiting for it at once."""
        query = urllib.parse.urlencode({key: value for key, value in (params or {}).items()
                                        if value is not None and value != ""})
        url = self.base_url + urllib.parse.quote(path) + (f"?{query}" if query else "")
        for attempt in range(2):
            status, reason, headers, body = await anyio.to_thread.run_sync(self._fetch, url, abandon_on_cancel=True)
            if status == 429 and attempt == 0:
                try:
                    wait = int(headers.get("Retry-After", ""))
                except ValueError:
                    wait = 60
                await anyio.sleep(wait if 1 <= wait <= 60 else 60)
                continue
            if 400 <= status < 500:
                raise DatacircleError(_words(status, reason, body), status)
            if not 200 <= status < 300:
                raise DatacircleError(f"Datacircle's API answered {status} {reason}".strip(), status)
            try:
                return json.loads(body)
            except ValueError:
                raise DatacircleError("Datacircle's API gave no JSON", status) from None
        raise AssertionError("unreachable")

    def _fetch(self, url: str) -> Tuple[int, str, Mapping[str, str], bytes]:
        request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as answer:
                return answer.status, answer.reason, answer.headers, answer.read()
        except urllib.error.HTTPError as error:
            return error.code, str(error.reason or ""), error.headers, error.read()
        except (urllib.error.URLError, OSError) as error:
            raise DatacircleError(f"Datacircle's API could not be reached: {getattr(error, 'reason', None) or error}") from None
