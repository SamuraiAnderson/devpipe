"""Connection dependencies are distinct from Session.at() view ancestry."""
from __future__ import annotations
from typing import Any
from .exceptions import SessionError, UnsupportedOperationError


def _derive(parent: Any, kind: str, params: dict[str, Any]) -> Any:
    root = parent.root
    root._check_alive()
    if root.kind != "ssh":
        raise UnsupportedOperationError(f"{root.kind} cannot host a {kind} session")
    workspace = getattr(root, "_workspace_owner", None)
    if workspace is not None:
        workspace._check_stop_requested(kind)
        return workspace._derive_session(root, kind, params)
    return _create_child(root, kind, params,
                         owned=not (root._context_depth or root._pool_owned))


def _create_child(parent: Any, kind: str, params: dict[str, Any], *, owned: bool) -> Any:
    if owned and parent._chain_transferred:
        raise SessionError("SSH ownership was transferred; use with/Workspace for shared devices",
                           session=parent)
    try:
        if kind == "adb":
            from ._adb import AdbSession
            child = AdbSession(**params, _host=parent._host_backend)
        elif kind == "wsl":
            from ._wsl import WslSession
            if parent.path_style != "windows":
                raise UnsupportedOperationError("WSL requires a Windows SSH host")
            child = WslSession(**params, _host=parent._host_backend)
        elif kind == "serial":
            from ._serial import SerialSession
            child = SerialSession(**params, _host=parent._host_backend)
        else:
            raise UnsupportedOperationError(f"unsupported child session: {kind}")
        parent._check_alive()
    except BaseException as exc:
        if "child" in locals():
            try:
                child.close()
            except Exception as cleanup:
                setattr(exc, "cleanup_error", cleanup)
        if owned:
            try:
                parent.close()
            except Exception as cleanup:
                setattr(exc, "cleanup_error", cleanup)
        raise
    child._via = parent
    child._owns_via = owned
    parent._dependents.add(child)
    if owned:
        parent._chain_transferred = True
    child._log_buffer.append(event="session_route", level="INFO", stream="system",
                            message=f"{parent.label} -> {child.label}",
                            fields={"via_session_id": parent.session_id})
    return child
