import re
import urllib.parse
import urllib.request
from html.parser import HTMLParser


SITE_URL = "https://zwyy.henu.edu.cn/v4/"
TERMS = (
    "pcTopFor",
    "space/pick",
    "Space/map",
    "Space/seat",
    "space/confirm",
    "authorization",
    "Bearer",
    "bearer",
    "aesjson",
    "axios",
    "baseURL",
)


class Parser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "script":
            attrs = dict(attrs)
            if attrs.get("src"):
                self.scripts.append(attrs["src"])


def fetch(opener, url):
    return opener.open(
        urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}),
        timeout=20,
    )


def report(text, label):
    lower = text.lower()
    hits = [term for term in TERMS if term.lower() in lower]
    print("%s terms=%s" % (label, ",".join(hits) if hits else "none"))
    for term in hits:
        pos = lower.find(term.lower())
        start = max(0, pos - 300)
        snippet = re.sub(r"\s+", " ", text[start : pos + 900])
        print("CONTEXT[%s] %s" % (term, snippet[:1200]))


opener = urllib.request.build_opener()
response = fetch(opener, SITE_URL)
html = response.read().decode("utf-8", "replace")
print("PAGE status=%s bytes=%s final_url=%s" % (response.status, len(html), response.geturl()))
print(re.sub(r"\s+", " ", html[:1200]))
parser = Parser()
parser.feed(html)
print("SCRIPT_COUNT=%s" % len(parser.scripts))
for src in parser.scripts:
    url = urllib.parse.urljoin(response.geturl() or SITE_URL, src)
    try:
        script_response = fetch(opener, url)
        script = script_response.read().decode("utf-8", "replace")
        print("SCRIPT url=%s status=%s bytes=%s" % (url, script_response.status, len(script)))
        report(script, "SCRIPT")
    except Exception as exc:
        print("SCRIPT_ERROR url=%s error=%s" % (url, type(exc).__name__))
