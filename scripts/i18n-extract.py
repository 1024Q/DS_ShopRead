"""Collect UI text (Simplified Chinese source strings) for the locale dictionaries.

Usage: python scripts/i18n-extract.py > tmp/i18n-corpus.json

Pieces of JSX text, string literals and template/f-strings that contain Chinese
are collected. Interpolations become numbered placeholders ({0}, {1} …) so that
runtime text such as "共 12 项" matches the pattern "共 {0} 项".
The output feeds scripts/i18n-build.mjs, which writes app/i18n/zh-TW.mjs.
"""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CJK = re.compile(r"[一-鿿]")


def pieces(text):
    """Split on runs of whitespace/newlines the way the browser collapses JSX text."""
    text = re.sub(r"\s+", " ", text).strip()
    return [text] if text and CJK.search(text) else []


def template(raw, opener, closer):
    """Turn `a ${x} b` / f"a {x} b" into a pattern with numbered placeholders."""
    out, depth, index, buf = [], 0, 0, []
    i = 0
    while i < len(raw):
        if raw.startswith(opener, i) and depth == 0:
            out.append("".join(buf))
            buf = []
            depth = 1
            i += len(opener)
            continue
        if depth:
            if raw[i] == "{":
                depth += 1
            elif raw[i] == closer:
                depth -= 1
                if depth == 0:
                    out.append("{%d}" % index)
                    index += 1
            i += 1
            continue
        buf.append(raw[i])
        i += 1
    out.append("".join(buf))
    return "".join(out)


def js(path, exact, patterns):
    s = path.read_text(encoding="utf-8")
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.S)
    s = re.sub(r"(?m)^\s*//.*$", "", s)
    for m in re.finditer(r"'((?:[^'\\\n]|\\.)*)'|\"((?:[^\"\\\n]|\\.)*)\"", s):
        for p in pieces((m.group(1) or m.group(2) or "").replace("\\n", " ")):
            exact.add(p)
    for m in re.finditer(r"`((?:[^`\\]|\\.)*)`", s, flags=re.S):
        raw = m.group(1)
        if "${" in raw:
            # Each literal fragment is also a candidate (JSX splits nodes on {}).
            pattern = re.sub(r"\s+", " ", template(raw, "${", "}")).strip()
            if CJK.search(pattern):
                patterns.add(pattern)
        else:
            for p in pieces(raw):
                exact.add(p)
    # JSX text between tags / braces.
    for m in re.finditer(r">([^<>{}]*[一-鿿][^<>{}]*)(?=[<{])", s):
        for p in pieces(m.group(1)):
            exact.add(p)
    for m in re.finditer(r"\}([^<>{}]*[一-鿿][^<>{}]*)(?=[<{])", s):
        for p in pieces(m.group(1)):
            exact.add(p)


def py(path, exact, patterns):
    s = path.read_text(encoding="utf-8")
    for m in re.finditer(r"(?<![\w])([rRbBuUfF]{0,2})(\"\"\"|'''|\"|')", s):
        pass
    for m in re.finditer(r"(?<![\w'\"])(f?)(\"(?:[^\"\\\n]|\\.)*\"|'(?:[^'\\\n]|\\.)*')", s):
        prefix, body = m.group(1), m.group(2)[1:-1]
        if not CJK.search(body):
            continue
        if prefix == "f" and "{" in body:
            pattern = re.sub(r"\s+", " ", template(body.replace("{{", "\0").replace("}}", "\1"), "{", "}"))
            pattern = pattern.replace("\0", "{").replace("\1", "}").strip()
            if CJK.search(pattern):
                patterns.add(pattern)
        else:
            for p in pieces(body):
                exact.add(p)


def main():
    exact, patterns = set(), set()
    for path in sorted((ROOT / "app").rglob("*")):
        if path.suffix in (".js", ".mjs") and ".test." not in path.name and path.parent.name != "i18n":
            js(path, exact, patterns)
    for path in sorted((ROOT / "lib").rglob("*.mjs")):
        js(path, exact, patterns)
    for path in sorted((ROOT / "config").glob("*.json")):
        for m in re.finditer(r'"((?:[^"\\]|\\.)*)"', path.read_text(encoding="utf-8")):
            for p in pieces(m.group(1)):
                exact.add(p)
    for path in sorted((ROOT / "backend").rglob("*.py")):
        if "tests" not in path.parts:
            py(path, exact, patterns)
    # Split exact strings on separators used for composition (· ； ， ：) as extra candidates.
    extra = set()
    for text in exact:
        for part in re.split(r"\s*[·；]\s*", text):
            if CJK.search(part) and part != text:
                extra.add(part.strip())
    exact |= extra
    code = re.compile(r"[`$=&|]|\?\s|=>|\breturn\b|\bnull\b|\)\s*[:?;]|^\W*$")
    exact = {t for t in exact if not code.search(t) and len(t) <= 300}
    patterns = {t for t in patterns if not re.search(r"[`$=&|]|=>", t) and len(t) <= 300}
    json.dump({"exact": sorted(exact), "patterns": sorted(patterns)}, sys.stdout, ensure_ascii=False, indent=0)


if __name__ == "__main__":
    main()
