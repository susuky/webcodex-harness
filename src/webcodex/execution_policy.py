"""Reject known external model-worker launches at WebCodex execution entry points."""

from __future__ import annotations

import ast
import base64
import re
import shlex
from pathlib import Path

WORKERS = {"codex", "codex-cli", "work", "chatgpt-work", "work-agent"}
SHELLS = {"bash", "sh", "dash", "zsh", "fish", "ksh", "cmd", "powershell", "pwsh"}
MESSAGE = "External Codex/Work workers are disabled in WebCodex."


def _name(value: str) -> str:
    name = value.strip("\"'").replace("\\", "/").rsplit("/", 1)[-1].casefold()
    return re.sub(r"\.(?:exe|com|cmd|bat|ps1|js|mjs|py)$", "", name)


def _worker(value: str) -> bool:
    return _name(value) in WORKERS or bool(
        re.search(r"(?:^|[/\\])@openai[/\\]codex(?:[/\\@]|$)", value, re.I)
    )


def is_worker_tool(name: str) -> bool:
    name = re.split(r"__|[/:.]", name.casefold())[-1].replace("-", "_")
    if name.startswith("subagent_") or name in {"spawn_agent", "spawn_subagent", "run_agent"}:
        return True
    return bool(
        re.fullmatch(
            r"(?:codex(?:_(?:reply|exec|run|resume|review|spawn|agent))?"
            r"|(?:work|chatgpt_work)(?:_(?:agent|subagent|spawn|exec|run|resume|reply))?"
            r"|(?:spawn|run|exec)_(?:codex|work|agent|subagent))",
            name,
        )
    )


def check_worker_tool(name: str) -> None:
    if is_worker_tool(name):
        raise ValueError(MESSAGE)


def _shell(source: str, cwd: Path | None, depth: int) -> None:
    lines, output, index = source.splitlines(keepends=True), [], 0
    while index < len(lines):
        line = lines[index]
        output.append(line)
        index += 1
        header = shlex.shlex(line, posix=False, punctuation_chars="<")
        header.whitespace_split = True
        try:
            tokens = list(header)
        except ValueError:
            continue
        if "<<" not in tokens or tokens.index("<<") + 1 >= len(tokens):
            continue
        marker = tokens[tokens.index("<<") + 1]
        delimiter = marker.lstrip("-").strip("\"'")
        body = []
        while index < len(lines) and lines[index].strip() != delimiter:
            body.append(lines[index])
            index += 1
        index += 1
        receiver = next((token for token in tokens if not re.match(r"^[A-Za-z_]\w*=", token)), "")
        if _name(receiver) in SHELLS | {"node", "nodejs"} or _name(receiver).startswith("python"):
            check_worker_script("".join(body), receiver, cwd=cwd, _depth=depth + 1)
    source = "".join(output)
    lexer = shlex.shlex(source, posix=False, punctuation_chars=";&|(){}\n")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    command = []
    try:
        for token in lexer:
            if token and all(char in ";&|(){}\n" for char in token):
                check_worker_command(command, cwd=cwd, _depth=depth + 1)
                command = []
            else:
                command.append(token.strip("\"'"))
    except ValueError as error:
        if str(error) not in {"No closing quotation", "No escaped character"}:
            raise
        # An incomplete interactive line is checked again when more input arrives.
        return
    check_worker_command(command, cwd=cwd, _depth=depth + 1)


def _powershell_pipeline_start(prefix: str) -> bool:
    if "$(" in prefix:
        return False
    quote, escaped, last = "", False, ""
    nesting = []
    closing = {")": "(", "]": "[", "}": "{"}
    for char in prefix:
        if escaped:
            escaped = False
        elif char == "`" and quote != "'":
            escaped, last = True, char
        elif quote:
            if char == quote:
                quote = ""
        elif char in "'\"":
            quote = last = char
        elif char == "#":
            return False
        elif char in "([{":
            nesting.append(char)
            last = char
        elif char in closing:
            if not nesting or nesting.pop() != closing[char]:
                return False
            last = char
        elif not char.isspace():
            last = char
    return not quote and not escaped and not nesting and last in {"", ";"}


def _powershell_here_strings(source: str, cwd: Path | None, depth: int) -> str:
    def replace(match: re.Match) -> str:
        original = match.group(0)
        if not _powershell_pipeline_start(source[: match.start()]):
            return original
        receiver = re.match(
            r"""[ \t]*\|[ \t\r\n]*(?:&[ \t]*)?("[^"\r\n]+"|'[^'\r\n]+'|[^\s;|(){}]+)""",
            source[match.end() :],
        )
        if receiver is None:
            return original
        language = _name(receiver[1])
        if language in {"iex", "invoke-expression"}:
            language = "powershell"
        if not (language in SHELLS | {"node", "nodejs", "bun"} or language.startswith("python")):
            return original
        body = match["body"]
        # Expanding strings keep the existing shell checks for evaluated expressions.
        if match["quote"] == '"' and ("$" in body or "`" in body):
            return original
        check_worker_script(body, language, cwd=cwd, _depth=depth + 1)
        return "''"

    return re.sub(
        r"""(?<!\x60)@(?P<quote>['"])[ \t]*\r?\n(?P<body>.*?)^(?P=quote)@""",
        replace,
        source,
        flags=re.MULTILINE | re.DOTALL,
    )


def check_worker_script(
    source: str, interpreter: str, *, cwd: Path | None = None, _depth: int = 0
) -> None:
    if _depth > 8:
        raise ValueError("Command wrappers exceed the supported nesting depth")
    language = _name(interpreter)
    if language in SHELLS:
        if language in {"powershell", "pwsh"}:
            source = _powershell_here_strings(source, cwd, _depth)
        _shell(source, cwd, _depth)
    elif language.startswith("python"):
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return
        aliases = {"subprocess": "subprocess", "os": "os", "asyncio": "asyncio"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                aliases.update({item.asname or item.name: item.name for item in node.names})
            elif isinstance(node, ast.ImportFrom) and node.module in {
                "subprocess",
                "os",
                "asyncio",
            }:
                aliases.update(
                    {item.asname or item.name: f"{node.module}.{item.name}" for item in node.names}
                )
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            function = ast.unparse(node.func)
            root, dot, rest = function.partition(".")
            function = aliases.get(root, root) + dot + rest
            if function not in {
                "subprocess.run",
                "subprocess.Popen",
                "subprocess.call",
                "subprocess.check_call",
                "subprocess.check_output",
                "os.system",
                "os.popen",
                "os.execv",
                "os.execvp",
                "os.execve",
                "os.execvpe",
                "asyncio.create_subprocess_exec",
                "asyncio.create_subprocess_shell",
            }:
                continue
            try:
                argument = ast.literal_eval(node.args[0])
                if function == "asyncio.create_subprocess_exec":
                    argument = [ast.literal_eval(arg) for arg in node.args]
            except (ValueError, TypeError, SyntaxError):
                continue
            if isinstance(argument, str):
                _shell(argument, cwd, _depth + 1)
            elif isinstance(argument, (list, tuple)) and all(
                isinstance(arg, str) for arg in argument
            ):
                check_worker_command(list(argument), cwd=cwd, _depth=_depth + 1)
    elif language in {"node", "nodejs", "bun"} and "child_process" in source:
        for match in re.finditer(
            r"\b(exec|execSync|spawn|spawnSync|execFile|execFileSync)\s*\(\s*(['\"])(.*?)\2(?:\s*,\s*(\[[^\]\n]*\]))?",
            source,
        ):
            method, _, command, arguments = match.groups()
            if method in {"exec", "execSync"}:
                _shell(command, cwd, _depth + 1)
            else:
                try:
                    args = ast.literal_eval(arguments) if arguments else []
                except (ValueError, SyntaxError):
                    args = []
                if isinstance(args, list) and all(isinstance(arg, str) for arg in args):
                    check_worker_command([command, *args], cwd=cwd, _depth=_depth + 1)


def check_worker_command(argv: list[str], *, cwd: Path | None = None, _depth: int = 0) -> None:
    while argv and re.match(r"^[A-Za-z_]\w*=", argv[0]):
        argv = argv[1:]
    if not argv:
        return
    if _depth > 8:
        raise ValueError("Command wrappers exceed the supported nesting depth")
    command, *args = argv
    name = _name(command)
    if _worker(command):
        raise ValueError(MESSAGE)
    if name in {
        "if",
        "while",
        "until",
        "!",
        "then",
        "do",
        "exec",
        "command",
        "call",
        "nohup",
        "setsid",
    }:
        check_worker_command(args, cwd=cwd, _depth=_depth + 1)
    elif name in {"env", "sudo", "nice", "timeout"}:
        value_options = {
            "env": {"-u", "--unset", "-C", "--chdir"},
            "sudo": {"-u", "--user", "-g", "--group", "-p", "--prompt", "-h", "--host"},
            "nice": {"-n", "--adjustment"},
            "timeout": {"-k", "--kill-after", "-s", "--signal"},
        }[name]
        index = 0
        while index < len(args):
            arg = args[index]
            if arg in value_options:
                index += 2
            elif arg.startswith("-") or "=" in arg:
                index += 1
            elif name == "timeout" and re.fullmatch(r"\d+(?:\.\d+)?[smhd]?", arg):
                index += 1
            else:
                break
        check_worker_command(args[index:], cwd=cwd, _depth=_depth + 1)
    elif (
        name in {"npx", "bunx", "uvx"}
        or name in {"npm", "pnpm", "yarn", "bun", "uv"}
        and any(arg in {"exec", "run", "dlx", "x"} for arg in args[:2])
    ):
        nested = args[:]
        while nested:
            arg = nested[0]
            if arg in {"exec", "run", "dlx", "x", "tool"} or arg in {
                "--",
                "-y",
                "--yes",
                "--no-install",
            }:
                nested.pop(0)
            elif (
                arg in {"-p", "--package", "--from", "--project", "--directory", "--with"}
                and len(nested) > 1
            ):
                if arg in {"-p", "--package", "--from", "--with"} and _worker(nested[1]):
                    raise ValueError(MESSAGE)
                nested = nested[2:]
            elif arg.startswith(("--package=", "--from=", "--with=")):
                if _worker(arg.split("=", 1)[1]):
                    raise ValueError(MESSAGE)
                nested.pop(0)
            elif arg.startswith("-"):
                nested.pop(0)
            else:
                break
        check_worker_command(nested, cwd=cwd, _depth=_depth + 1)
    elif name == "tunnel-client" and args and _name(args[0]) in WORKERS:
        raise ValueError(MESSAGE)
    elif name in {"start-process", "start"}:
        index = next(
            (index + 1 for index, arg in enumerate(args) if arg.casefold() == "-filepath"), 0
        )
        check_worker_command(args[index : index + 1], cwd=cwd, _depth=_depth + 1)
    elif name in {"iex", "invoke-expression"} and args:
        _shell(args[0], cwd, _depth + 1)
    elif name in SHELLS or name.startswith("python") or name in {"node", "nodejs", "bun"}:
        skip_value = False
        for index, arg in enumerate(args):
            if skip_value:
                skip_value = False
                continue
            if name in {"powershell", "pwsh"} and arg.casefold() in {
                "-executionpolicy",
                "-ep",
                "-version",
                "-inputformat",
                "-outputformat",
                "-windowstyle",
                "-w",
                "-workingdirectory",
            }:
                skip_value = True
                continue
            if arg.casefold() in {
                "-c",
                "-lc",
                "/c",
                "-command",
                "-e",
                "--eval",
                "-p",
                "--print",
            } and index + 1 < len(args):
                check_worker_script(args[index + 1], name, cwd=cwd, _depth=_depth + 1)
                return
            if (
                name in {"powershell", "pwsh"}
                and arg.casefold() in {"-encodedcommand", "-enc"}
                and index + 1 < len(args)
            ):
                try:
                    decoded = base64.b64decode(args[index + 1], validate=True).decode("utf-16-le")
                except (ValueError, UnicodeError):
                    return
                check_worker_script(decoded, name, cwd=cwd, _depth=_depth + 1)
                return
            if arg == "-m" and index + 1 < len(args):
                if _worker(args[index + 1]):
                    raise ValueError(MESSAGE)
                return
            if not arg.startswith("-"):
                if _worker(arg):
                    raise ValueError(MESSAGE)
                script = Path(arg)
                if not script.is_absolute() and cwd is not None:
                    script = cwd / script
                try:
                    if script.is_file() and script.stat().st_size <= 2 * 1024 * 1024:
                        check_worker_script(
                            script.read_text(encoding="utf-8"), name, cwd=cwd, _depth=_depth + 1
                        )
                except (OSError, UnicodeError):
                    pass
                return


def check_worker_input(argv: list[str], previous: str, chars: str) -> str:
    if not argv or not (
        _name(argv[0]) in SHELLS | {"node", "nodejs"} or _name(argv[0]).startswith("python")
    ):
        return ""
    # Explicit programs receive stdin as data. Bare interpreters and '-'/ '-s' read code.
    args = [arg.casefold() for arg in argv[1:]]
    interactive = "-i" in args or "--interactive" in args or "/k" in args
    if not interactive and any(
        arg
        in {
            "-c",
            "-lc",
            "/c",
            "-command",
            "-encodedcommand",
            "-enc",
            "-m",
            "-e",
            "--eval",
            "-p",
            "--print",
            "-file",
        }
        for arg in args
    ):
        if not ("-command" in args and args[args.index("-command") + 1 :] == ["-"]):
            return ""
    if (
        not interactive
        and "-s" not in args
        and any(arg != "-" and not arg.startswith("-") for arg in args)
    ):
        return ""
    if chars == "\x03":
        return ""
    pending = previous
    for char in chars.replace("\r\n", "\n").replace("\r", "\n"):
        if char in {"\b", "\x7f"}:
            pending = pending[:-1]
        elif char == "\x15":
            pending = pending.rsplit("\n", 1)[0] + "\n" if "\n" in pending else ""
        else:
            pending += char
    if len(pending) > 65536:
        raise ValueError("Terminal command input exceeds 64 KiB")
    check_worker_script(pending, argv[0])
    return pending
