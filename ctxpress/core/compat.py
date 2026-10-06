"""String semantics used when porting JavaScript reference implementations."""
import json


def js_length(text):
    """JavaScript String.length: UTF-16 code units, including unpaired surrogates."""
    return len(text.encode("utf-16-le", errors="surrogatepass")) // 2


def js_stringify(text):
    """JSON.stringify of the value a JSON text holds (compact, non-ASCII literal), for originals that measure a
    parsed object while hosts send argument strings that may be spaced or escaped; unreadable text is kept."""
    try:
        return json.dumps(json.loads(text), ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return text
