TRANSIENT_ERROR_NAMES = {
    'APIConnectionError', 'APITimeoutError', 'RateLimitError', 'InternalServerError',
    'ConnectError', 'ConnectTimeout', 'ReadError', 'ReadTimeout', 'RemoteProtocolError',
    'ServiceUnavailableError', 'TimeoutException',
}


def is_transient_error(exc: BaseException) -> bool:
    """Recognize provider/network failures without retrying invalid app definitions."""
    current: BaseException | None = exc
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        if isinstance(current, (TimeoutError, ConnectionError)) or type(current).__name__ in TRANSIENT_ERROR_NAMES:
            return True
        current = current.__cause__ or current.__context__
    return False
