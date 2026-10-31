class PageReadError(ValueError):
    """A page/navigation/authentication failure that can be retried from home."""


class LoginPending(ValueError):
    """Data synchronization is waiting for login or human verification."""
