"""Lecteur YAML réduit au sous-ensemble qu'utilise config.yaml de no-mistakes.

Gère : maps imbriquées, listes de scalaires ou de maps, listes en ligne
`[a, b]`, guillemets, commentaires, scalaires bloc `|` et `>`. Tout reste
une chaîne, sauf true/false et les entiers. Pas d'ancres, pas de maps en ligne.
"""


def _strip_comment(line):
    quote = None
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
            return line[:i]
    return line


def _scalar(text):
    text = text.strip()
    if not text:
        return None
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        return [_scalar(part) for part in inner.split(",")] if inner else []
    low = text.lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("null", "~"):
        return None
    try:
        return int(text)
    except ValueError:
        return text


def _split_key(text):
    """Renvoie (clé, reste) si `text` est une entrée de map, sinon None."""
    if text[:1] in "\"'":
        return None
    idx = text.find(":")
    while idx != -1:
        if idx + 1 == len(text) or text[idx + 1] in " \t":
            return text[:idx].strip(), text[idx + 1:].strip()
        idx = text.find(":", idx + 1)
    return None


def _lines(source):
    out = []
    block_indent = None
    for raw in source.splitlines():
        indent = len(raw) - len(raw.lstrip(" "))
        if block_indent is not None:
            if not raw.strip() or indent > block_indent:
                continue
            block_indent = None
        text = _strip_comment(raw).rstrip()
        if not text.strip():
            continue
        body = text.strip()
        if body.endswith(": |") or body.endswith(": >") or body.endswith(": |-") or body.endswith(": >-"):
            block_indent = indent
            body = body[: body.rfind(":") + 1]
        out.append((indent, body))
    return out


def _parse(lines, pos, indent):
    if pos >= len(lines):
        return None, pos
    if lines[pos][1].startswith("- ") or lines[pos][1] == "-":
        items = []
        while pos < len(lines) and lines[pos][0] == indent and lines[pos][1].startswith("-"):
            body = lines[pos][1][1:].strip()
            entry = _split_key(body) if body else None
            if entry:
                # Élément de liste qui ouvre une map : on le réinjecte décalé.
                inner = indent + 2
                lines[pos] = (inner, body)
                value, pos = _parse(lines, pos, inner)
                items.append(value)
            else:
                items.append(_scalar(body))
                pos += 1
        return items, pos
    result = {}
    while pos < len(lines) and lines[pos][0] == indent:
        entry = _split_key(lines[pos][1])
        if not entry:
            pos += 1
            continue
        key, rest = entry
        pos += 1
        if rest:
            result[key] = _scalar(rest)
        elif pos < len(lines) and (
            lines[pos][0] > indent or (lines[pos][0] == indent and lines[pos][1].startswith("-"))
        ):
            result[key], pos = _parse(lines, pos, lines[pos][0])
        else:
            result[key] = None
        # Lignes plus indentées que prévu sans parent : on les saute.
        while pos < len(lines) and lines[pos][0] > indent:
            pos += 1
    return result, pos


def load(source):
    lines = _lines(source)
    if not lines:
        return {}
    value, _ = _parse(lines, 0, lines[0][0])
    return value if isinstance(value, dict) else {}
