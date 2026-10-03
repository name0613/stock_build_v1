"""One exclusion predicate for UI, queues and daily completion."""
from .models import StockRefreshIssue

REFRESH_NO_DATA_LIMIT = 5
STOCK_COVERAGE_GAP_CODES = frozenset({
    "EMPTY_RESPONSE_UNVERIFIED", "PARTIAL_RESPONSE_UNVERIFIED", "PARTIAL_OBSERVATION_COVERAGE",
})


def exclusion_predicate():
    return StockRefreshIssue.no_data_attempts >= REFRESH_NO_DATA_LIMIT
