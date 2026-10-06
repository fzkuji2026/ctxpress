"""Install ctxpress into Codex so that plain `codex` uses it.

`ctxpress install codex` adds a marked block to your shell start-up files (bash ~/.bashrc, zsh ~/.zshrc,
PowerShell $PROFILE) that defines a `codex` function calling `ctxpress codex -- <your args>`. Typing `codex`
then:
  1. starts the ctxpress proxy inside that process, on a free local port;
  2. starts the real Codex with a separate profile for the run (and request compression off), so
     every model request goes through the active method;
  3. stops the proxy when Codex exits.
Nothing keeps running in the background, Codex itself and your config.toml are not modified, and several
Codex windows each get their own proxy. Switch the method with `ctxpress use <Method>`; `ctxpress use
NoCompaction` passes requests through unchanged; `ctxpress uninstall codex` removes the block.
"""
from __future__ import annotations
import os, re
from ctxpress import settings

BEGIN, END = "# >>> ctxpress >>>", "# <<< ctxpress <<<"
POSIX = 'codex() { ctxpress codex -- "$@"; }'
POWERSHELL = "function codex { ctxpress codex -- @args }"


def shell_files():
    """Start-up files to edit: the ones that exist (or the shell's default when none exist)."""
    home = os.path.expanduser("~")
    files = []
    for name in (".bashrc", ".zshrc"):
        p = os.path.join(home, name)
        if os.path.exists(p):
            files.append((p, POSIX))
    if os.name == "nt":
        docs = os.path.join(home, "Documents")
        for sub in ("PowerShell", "WindowsPowerShell"):
            files.append((os.path.join(docs, sub, "Microsoft.PowerShell_profile.ps1"), POWERSHELL))
    elif not files:
        shell = os.path.basename(os.environ.get("SHELL", "bash"))
        files.append((os.path.join(home, ".zshrc" if shell == "zsh" else ".bashrc"), POSIX))
    return files


def strip(text):
    return re.sub(r"\n?" + re.escape(BEGIN) + r".*?" + re.escape(END) + r"\n?", "\n", text, flags=re.S).rstrip("\n") + ("\n" if text.strip() else "")


def install(method="NoCompaction", args=None, upstream=None, files=None):
    from ctxpress.methods import build
    build({"class": method, "args": args or {}})
    cfg = settings.load()
    cfg.update(method=method, args=args or {})
    if upstream:
        cfg["upstream"] = upstream
    settings.save(cfg)
    done = []
    for path, line in files or shell_files():
        os.makedirs(os.path.dirname(path), exist_ok=True)
        text = strip(open(path, encoding="utf-8").read()) if os.path.exists(path) else ""
        block = f"{BEGIN}\n# `codex` runs through ctxpress (ctxpress use <Method> to switch; ctxpress uninstall codex to remove)\n{line}\n{END}\n"
        with open(path, "w", encoding="utf-8") as fh:
            fh.write((text.rstrip("\n") + "\n\n" if text.strip() else "") + block)
        done.append(path)
    return done


def uninstall(files=None):
    from ctxpress.hosts.codex.launch import codex_home, PROFILE
    prof = os.path.join(codex_home(), f"{PROFILE}.config.toml")
    if os.path.exists(prof):
        os.remove(prof)
    done = []
    for path, _ in files or shell_files():
        if os.path.exists(path):
            text = open(path, encoding="utf-8").read()
            if BEGIN in text:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(strip(text))
                done.append(path)
    return done


def use(method, args=None):
    from ctxpress.methods import build
    build({"class": method, "args": args or {}})              # fail now on a bad method / arguments
    cfg = settings.load(); cfg.update(method=method, args=args or {}); settings.save(cfg)
    return cfg
