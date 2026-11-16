"""Role-based authorization helpers shared by every blueprint."""
from __future__ import annotations

from functools import wraps
from typing import Any, Callable

from flask import abort, jsonify, request
from flask_login import current_user, login_required


def permission_required(permission: str) -> Callable:
    """Require a signed-in user whose roles grant ``permission``."""

    def decorator(view: Callable) -> Callable:
        @wraps(view)
        @login_required
        def wrapper(*args: Any, **kwargs: Any):
            if not current_user.can(permission):
                if request.accept_mimetypes.best == "application/json" or request.is_json:
                    return jsonify({"error": f"Your role does not allow {permission}."}), 403
                abort(403)
            return view(*args, **kwargs)

        return wrapper

    return decorator


def admin_required(view: Callable) -> Callable:
    @wraps(view)
    @login_required
    def wrapper(*args: Any, **kwargs: Any):
        if not current_user.is_admin:
            abort(403)
        return view(*args, **kwargs)

    return wrapper
