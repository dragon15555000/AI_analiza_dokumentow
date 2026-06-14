import functools
import random
import time
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


def retry_api_call(
    attempts: int = 5,
    base_delay: float = 1.0,
    max_delay: float = 10.0,
    retry_statuses: set[int] | None = None,
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    if retry_statuses is None:
        retry_statuses = {429, 500, 502, 503, 504}

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> T:
            last_error: Exception | None = None

            for attempt in range(attempts):
                try:
                    return func(*args, **kwargs)
                except Exception as exc:
                    last_error = exc
                    status = getattr(exc, "status_code", None)
                    response = getattr(exc, "response", None)
                    if response is not None:
                        status = getattr(response, "status_code", status)

                    if status not in retry_statuses or attempt == attempts - 1:
                        raise

                    delay = min(max_delay, base_delay * (2**attempt))
                    delay += random.uniform(0, 0.25)
                    time.sleep(delay)

            raise last_error  # defensive, unreachable

        return wrapper

    return decorator
