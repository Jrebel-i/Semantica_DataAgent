"""Errors raised when a configured storage backend cannot serve a request."""


class StorageUnavailable(ConnectionError):
    """A storage backend could not be reached or stopped responding."""
