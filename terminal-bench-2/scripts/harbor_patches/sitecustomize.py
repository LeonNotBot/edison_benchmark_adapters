from __future__ import annotations

import os
import shlex
from pathlib import Path
from urllib.parse import urlsplit


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _patch_terminus2_tool_install() -> None:
    if not _truthy(os.environ.get("EDISON_TB2_PATCH_TERMINUS_APT")):
        return

    try:
        from harbor.agents.terminus_2.tmux_session import TmuxSession
    except Exception:
        return

    timeout = os.environ.get("EDISON_TB2_TOOL_INSTALL_TIMEOUT_SEC")
    budget = os.environ.get("EDISON_TB2_TOOL_INSTALL_BUDGET_SEC")
    if timeout:
        try:
            TmuxSession._TOOL_INSTALL_TIMEOUT_SEC = int(float(timeout))
        except ValueError:
            pass
    if budget:
        try:
            TmuxSession._TOOL_INSTALL_BUDGET_SEC = int(float(budget))
        except ValueError:
            pass

    mirror = (os.environ.get("EDISON_TB2_APT_MIRROR") or "").strip().rstrip("/")
    if not mirror:
        return

    original = TmuxSession._get_combined_install_command

    def patched_get_combined_install_command(self, system_info, tools):
        command = original(self, system_info, tools)
        if system_info.get("package_manager") != "apt-get" or not command:
            return command

        mirror_quoted = shlex.quote(mirror)
        security_mirror = mirror
        if mirror.endswith("/debian"):
            security_mirror = f"{mirror}-security"
        security_mirror_quoted = shlex.quote(security_mirror)

        replace_sources = (
            "for f in /etc/apt/sources.list /etc/apt/sources.list.d/debian.sources; do "
            "[ -f \"$f\" ] || continue; "
            f"sed -i "
            f"-e 's|http://deb.debian.org/debian|{mirror_quoted}|g' "
            f"-e 's|https://deb.debian.org/debian|{mirror_quoted}|g' "
            f"-e 's|http://security.debian.org/debian-security|{security_mirror_quoted}|g' "
            f"-e 's|https://security.debian.org/debian-security|{security_mirror_quoted}|g' "
            "\"$f\"; "
            "done"
        )
        return f"{replace_sources} && {command}"

    TmuxSession._get_combined_install_command = patched_get_combined_install_command


def claude_apt_setup_command(mirror: str) -> str:
    """Configure only the ephemeral agent container; retain apt signature checks."""
    mirror = mirror.strip().rstrip("/")
    setup = ""
    if mirror:
        parsed = urlsplit(mirror)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or any(char in mirror for char in "|&\\\n\r")):
            raise ValueError("container_apt_mirror must be an HTTP(S) repository URL without credentials or query")
        security = f"{mirror}-security" if mirror.endswith("/debian") else mirror
        replacements = []
        for scheme in ("http", "https"):
            for host in ("deb.debian.org", "security.debian.org"):
                replacements.append(f"s|{scheme}://{host}/debian-security|{security}|g")
            replacements.append(f"s|{scheme}://deb.debian.org/debian|{mirror}|g")
        setup = (
            'for f in /etc/apt/sources.list /etc/apt/sources.list.d/debian.sources; do '
            '[ -f "$f" ] || continue; sed -i '
            + " ".join(f"-e {shlex.quote(expr)}" for expr in replacements)
            + ' "$f"; done; '
        )
    options = ('Acquire::Retries "3";\nAcquire::http::Timeout "20";\n'
               'Acquire::https::Timeout "20";\nAPT::Update::Error-Mode "any";\n')
    return (
        "set -e; if command -v apt-get >/dev/null 2>&1; then "
        + setup + "mkdir -p /etc/apt/apt.conf.d; printf '%s' " + shlex.quote(options)
        + " > /etc/apt/apt.conf.d/80edison-network; "
        + "echo '[edison-agent-setup] apt download retries=3; container source configuration applied'; fi"
    )


def _patch_claude_code_install() -> None:
    patch_apt = _truthy(os.environ.get("EDISON_TB4_PATCH_CLAUDE_APT"))
    cache_dir = os.environ.get("EDISON_TB4_CLAUDE_CACHE_DIR", "")
    if not patch_apt and not cache_dir:
        return
    from harbor.agents.installed.claude_code import ClaudeCode

    original = ClaudeCode.install
    if getattr(original, "_edison_apt_patch", False):
        return

    async def install(self, environment):
        if patch_apt:
            await self.exec_as_root(environment, command=claude_apt_setup_command(
                os.environ.get("EDISON_TB4_CLAUDE_APT_MIRROR", "")
            ))
        if cache_dir:
            from claude_cache import install_cached_claude
            version = os.environ["EDISON_TB4_CLAUDE_CACHE_VERSION"]
            if getattr(self, "_version", None) not in (None, version):
                raise RuntimeError("Claude cache version differs from configured agent version")
            return await install_cached_claude(self, environment, Path(cache_dir), version)
        return await original(self, environment)

    install._edison_apt_patch = True
    ClaudeCode.install = install


_patch_terminus2_tool_install()
_patch_claude_code_install()
