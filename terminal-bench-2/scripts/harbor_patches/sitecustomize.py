from __future__ import annotations

import os
import shlex


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


_patch_terminus2_tool_install()
