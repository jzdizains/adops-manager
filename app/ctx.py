"""Request-scoped context with no imports of its own (models.py reads it at insert time).

OWNER: the user id anything created during this request belongs to — the workspace in
view (the user themselves, or the one the super admin is looking at). Set by the auth
middleware for every logged-in request; None in background jobs, where the code passes
the owner explicitly."""
from contextvars import ContextVar

OWNER: ContextVar = ContextVar("adops_owner", default=None)
# VIEW: whose workspace this request is LOOKING at — None means everyone's (the super
# admin on "Everyone"); for a buyer it equals OWNER
VIEW: ContextVar = ContextVar("adops_view", default=None)


def owner_default(_ctx=None):
    """SQLAlchemy column default: the owner of whatever is being inserted right now."""
    return OWNER.get()
