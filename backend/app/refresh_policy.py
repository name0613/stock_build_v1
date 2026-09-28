"""One exclusion predicate for UI, queues and daily completion."""
from .models import StockRefreshIssue

REFRESH_NO_DATA_LIMIT = 5


def exclusion_predicate():
    return StockRefreshIssue.no_data_attempts >= REFRESH_NO_DATA_LIMIT
