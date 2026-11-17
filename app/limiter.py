from slowapi import Limiter
from slowapi.util import get_remote_address

from app.config import get_settings

# Prod: counter rate-limit dibagi antar-worker lewat Redis. Dev/regression
# (USE_FAKEREDIS=true) tetap memory:// — tidak butuh Redis untuk jalan.
_settings = get_settings()


def build_limiter(settings):
    # swallow_errors: Redis putus sesaat tidak boleh membuat login 500; limit
    # jatuh ke memory per-proses sampai Redis pulih.
    return Limiter(
        key_func=get_remote_address,
        storage_uri=None if settings.USE_FAKEREDIS else settings.REDIS_URL,
        swallow_errors=True,
    )


limiter = build_limiter(_settings)
