"""Query source before expanding it; keep every omission explicitly recoverable."""
from __future__ import annotations

import re
from ctxpress.evolve.runtime import cli
from ctxpress.methods.readless_policies.seed import read_target
from ctxpress.tools.definition import definitions, find


TOOL = "python3 -m " + (__spec__.name if __spec__ else __name__)
READ_LINES = 64
READ_CHARS = 4800
MAP_ENTRIES = 18
SEARCH_MATCHES = 10
_NUMBERED = re.compile(r"^\s*\d+\t")

GUIDANCE = (
    "Keep source reads tied to the next change or verification. For discovery, prefer `" + TOOL +
    " seek 'NAME|OTHER_NAME' FILE [FILE ...]`: one bounded answer with matching source, two context lines, "
    "and enclosing definition names/spans across the chosen files. Use rg --files / rg -l to locate files. "
    "Then `" + TOOL + " read FILE NAME` reads the actual implementation (qualified names work); "
    "`" + TOOL + " read FILE A:B` reads a known range directly. No preview is required for a known location. "
    "Prefer these to chained sed/cat surveys. Reads return up to 64 lines / 4800 characters with exact "
    "continuation commands. Several names or ranges in one read share that budget and overlapping ranges merge. "
    "For an unfamiliar file or API comparison, `" + TOOL + " map FILE [REGEX] [OFFSET]` shows qualified names, "
    "signatures, brief docstrings and spans without bodies. A read without a selection maps long files. "
    "Expand only code needed to implement or check the task; do not automatically page through files or "
    "read every search result. Before editing, read the relevant body and enough context to preserve behavior. "
    "Reuse earlier reads unless the file changed. Snippets/maps are partial, not evidence of absent behavior; "
    "follow continuation commands or use explicit sed ranges when more source is needed."
)


def _quote(value):
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _command(path, start, end):
    return TOOL + " read " + _quote(path) + " " + str(start) + ":" + str(end)


def _load(path):
    with open(path, encoding="utf-8", errors="replace") as handle:
        return handle.read().splitlines()


def _excerpt(path, lines, start, end, line_limit=READ_LINES, char_limit=READ_CHARS):
    end = min(end, len(lines))
    if start > end:
        return path + ": no lines in requested range (" + str(len(lines)) + " total)."
    output = [path + ":" + str(start) + "-" + str(end)]
    used = len(output[0]) + 1
    # Reserve both a continuation and an escape for a single enormous source line.
    reserve = 180 + len(_command(path, start, end)) + len(_quote(path))
    next_line = start
    shortened = None
    for number in range(start, min(end + 1, start + line_limit)):
        raw = lines[number - 1]
        prefix = str(number) + "\t"
        room = char_limit - reserve - used - len(prefix)
        if room < 30:
            break
        if len(raw) + 1 > room:
            if number != start:
                break
            output.append(prefix + raw[:max(1, room - 25)] + " ... [line shortened]")
            shortened = number
        else:
            output.append(prefix + raw)
        used += len(output[-1]) + 1
        next_line = number + 1
    if shortened is not None:
        number = str(shortened)
        output.append("[Full line: sed -n '" + number + "p' " + _quote(path) + "]")
    if next_line <= end:
        output.append("[" + str(end - next_line + 1) + " lines omitted; continue: " +
                      _command(path, next_line, end) + "]")
    return "\n".join(output)


def _signature(lines, start, end):
    index = start - 1
    if lines[index].lstrip().startswith("@"):
        for number in range(index + 1, min(end, start + 30)):
            if re.match(r"\s*(?:async\s+)?(?:def|class)\s", lines[number]):
                index = number
                break
    parts = [lines[index].strip()]
    # Preserve useful multiline argument lists without including implementation.
    while (index + 1 < min(end, start + 6) and
           (parts[-1].endswith(("(", ",", "\\")) or
            " ".join(parts).count("(") > " ".join(parts).count(")"))):
        index += 1
        parts.append(lines[index].strip())
    signature = " ".join(parts)
    if len(signature) > 145:
        signature = signature[:142] + "..."
    doc = ""
    if index + 1 < end:
        match = re.match(r"(?:[rRuU]{0,2})?(\"\"\"|''')(.*)", lines[index + 1].strip())
        if match:
            doc = match.group(2).split(match.group(1), 1)[0].strip()
            if not doc and index + 2 < end:
                following = lines[index + 2].strip()
                if not following.startswith(match.group(1)):
                    doc = following.split(match.group(1), 1)[0].strip()
    return signature, doc if len(doc) <= 90 else doc[:87] + "..."


def _map(path, lines, pattern="", offset=0):
    entries = definitions(path, lines)
    matcher = re.compile(pattern, re.IGNORECASE) if pattern else None
    previews = []
    for name, start, end in entries:
        signature, doc = _signature(lines, start, end)
        if matcher is None or matcher.search(name + " " + signature + " " + doc):
            previews.append((name, start, end, signature, doc))
    output = [path + " (" + str(len(lines)) + " lines; " + str(len(previews)) +
              " matching definitions; bodies and other top-level code omitted)"]
    if not previews:
        if not entries and not pattern:
            return _excerpt(path, lines, 1, len(lines), 32)
        output.append("[No matching definitions. Read source: " +
                      _command(path, 1, min(READ_LINES, len(lines))) + "]")
        return "\n".join(output)
    page = previews[offset:offset + MAP_ENTRIES]
    for name, start, end, signature, doc in page:
        output.append(str(start) + ":" + str(end) + " " + name[:100] + " | " + signature)
        if doc:
            output.append("  doc: " + doc)
    remaining = len(previews) - offset - len(page)
    if remaining > 0:
        output.append("[" + str(remaining) + " more; narrow REGEX or continue: " + TOOL +
                      " map " + _quote(path) + " " + _quote(pattern) + " " +
                      str(offset + len(page)) + "]")
    elif not page:
        output.append("[End of map.]")
    output.append("[Body: " + TOOL + " read " + _quote(path) + " NAME; source: " +
                  _command(path, 1, min(READ_LINES, len(lines))) + "]")
    return "\n".join(output)


def map_file(argv):
    if not 1 <= len(argv) <= 3:
        return "Usage: " + TOOL + " map FILE [REGEX] [OFFSET]"
    path = argv[0]
    try:
        offset = int(argv[2]) if len(argv) > 2 else 0
        if offset < 0:
            return "OFFSET must be nonnegative."
        return _map(path, _load(path), argv[1] if len(argv) > 1 else "", offset)
    except (OSError, ValueError, re.error) as error:
        return path + ": " + str(error)


def read(argv):
    if not 1 <= len(argv) <= 7:
        return "Usage: " + TOOL + " read FILE [NAME|A:B ...] (up to six selections; shared budget)"
    path = argv[0]
    try:
        lines = _load(path)
    except OSError as error:
        return path + ": " + str(error)
    if not lines:
        return path + ": empty file"
    if len(argv) == 1:
        if len(lines) <= READ_LINES and sum(len(line) + 1 for line in lines) <= READ_CHARS - 400:
            return _excerpt(path, lines, 1, len(lines))
        return _map(path, lines)
    spans, errors = [], []
    defs = None
    for selection in argv[1:]:
        span = re.fullmatch(r"(\d+)(?:[:-](\d*))?", selection)
        if span:
            start = int(span.group(1))
            end = int(span.group(2)) if span.group(2) else start + READ_LINES - 1
            if start < 1 or end < start:
                errors.append("Invalid range " + selection[:80] + "; use positive inclusive A:B.")
            else:
                spans.append((start, min(end, len(lines))))
            continue
        if defs is None:
            defs = definitions(path, lines)
        matches = find(defs, selection)
        if not matches:
            errors.append("No definition named " + selection[:100] + "; locate it with " +
                          TOOL + " seek " + _quote(selection[:100]) + " " + _quote(path))
        elif len(matches) > 1:
            choices = ", ".join(name[:100] + " " + str(start) + ":" + str(end)
                                for name, start, end in matches[:8])
            errors.append("Ambiguous " + selection[:100] + ": " + choices +
                          ("; more matches omitted" if len(matches) > 8 else "") +
                          ". Qualify the name or choose a range.")
        else:
            name, start, end = matches[0]
            for unused in range(5):
                if start <= 1 or not re.match(r"\s*(?://|#|/\*|\*|\*/)", lines[start - 2]):
                    break
                start -= 1
            spans.append((start, end))
    merged = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    count = max(1, len(merged) + len(errors))
    output = [path + ": " + error for error in errors]
    for start, end in merged:
        output.append(_excerpt(path, lines, start, end, max(1, READ_LINES // count),
                               max(500, READ_CHARS // count)))
    return "\n".join(output)


def seek(argv):
    """Search several selected files, returning bounded source rather than long bodies."""
    usage = "Usage: " + TOOL + " seek REGEX FILE [FILE ...] [--page OFFSET] (up to 12 files)"
    args = list(argv)
    offset = 0
    if len(args) >= 2 and args[-2] == "--page":
        try:
            offset = int(args[-1])
        except ValueError:
            return "Page OFFSET must be nonnegative."
        args = args[:-2]
    if not 2 <= len(args) <= 13 or offset < 0:
        return usage
    pattern, paths = args[0], list(dict.fromkeys(args[1:]))
    try:
        matcher = re.compile(pattern)
    except re.error as error:
        return "Invalid search regex: " + str(error)
    files, hits, errors = {}, [], []
    for path in paths:
        try:
            files[path] = _load(path)
        except OSError as error:
            errors.append(path + ": " + str(error))
            continue
        hits.extend((path, number) for number, line in enumerate(files[path], 1) if matcher.search(line))
    command = TOOL + " seek " + _quote(pattern) + " " + " ".join(_quote(path) for path in paths)
    output = ["Search: " + str(len(hits)) + " matching lines; snippets only (long lines marked [cut])."]
    output.extend(error[:240] for error in errors)
    used = sum(len(part) + 1 for part in output)
    budget = max(1000, READ_CHARS - len(command) - 260)
    cache = {}
    previous_path, previous_end = None, 0
    next_offset = offset
    for index in range(offset, min(len(hits), offset + SEARCH_MATCHES)):
        path, number = hits[index]
        lines = files[path]
        if path == previous_path and number <= previous_end:
            next_offset = index + 1
            continue
        if path not in cache:
            cache[path] = definitions(path, lines)
        scopes = [entry for entry in cache[path] if entry[1] <= number <= entry[2]]
        scope = min(scopes, key=lambda entry: entry[2] - entry[1]) if scopes else None
        start, end = max(1, number - 2), min(len(lines), number + 2)
        if path == previous_path:
            start = max(start, previous_end + 1)
        header = path + ":" + str(start) + "-" + str(end)
        if scope:
            header += " in " + scope[0][:100] + " (" + str(scope[1]) + ":" + str(scope[2]) + ")"
        block = [header]
        for line_number in range(start, end + 1):
            raw = lines[line_number - 1]
            block.append(str(line_number) + "\t" + (raw if len(raw) <= 220 else raw[:210] + " ... [cut]"))
        text = "\n".join(block)
        if used + len(text) + 1 > budget:
            break
        output.append(text)
        used += len(text) + 1
        previous_path, previous_end = path, end
        next_offset = index + 1
    if next_offset < len(hits):
        output.append("[" + str(len(hits) - next_offset) + " matches not shown; narrow REGEX or continue: " +
                      command + " --page " + str(next_offset) + "]")
    elif offset >= len(hits):
        output.append("[No matches on this page.]" if hits else "[No matching lines in readable files.]")
    output.append("[Read a needed enclosing NAME or A:B with " + TOOL + " read FILE SELECTION.]")
    return "\n".join(output)


def view(cmd, out, ctx):
    target = read_target(cmd)
    if target is None or not re.match(r"^\s*(?:cat|nl\s+-ba)\s", cmd):
        return None
    path, first, numbered = target
    if path.replace("\\", "/").rsplit("/", 1)[-1].upper() in ("AGENTS.MD", "CLAUDE.MD", "SKILL.MD"):
        return None
    lines = out.splitlines()
    if len(lines) <= READ_LINES and len(out) <= READ_CHARS:
        return None
    if numbered:
        lines = [_NUMBERED.sub("", line) for line in lines]
    result = _map(path, lines)
    result += "\n[Whole-file preview; use read FILE NAME or A:B for needed source, or explicit sed ranges.]"
    return result if len(result) < len(out) else None


TOOLS = {"seek": seek, "read": read, "map": map_file}


if __name__ == "__main__":
    raise SystemExit(cli(TOOLS))
