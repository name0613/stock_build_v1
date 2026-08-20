# NAS deployment evidence

Sanitized health, summary, data-status and timing responses.

{
  "url": "http://192.168.31.138:18080",
  "health": {
    "status": "ok",
    "service": "api",
    "database": "ok",
    "score_version": "s-only-v2",
    "formula_hash": "d2e96a338ed34348372a0657f7454df9b943b2885a78696e1c4f08c39710f45c",
    "timezone": "Asia/Taipei"
  },
  "worker_health": {
    "status": "ok",
    "ready": true,
    "age_seconds": 2,
    "overdue": false,
    "heartbeat": {
      "status": "idle",
      "ready": true,
      "scheduler_started_at": "2026-08-20T17:03:28.231242+00:00",
      "last_heartbeat_at": "2026-08-20T17:08:50.143594+00:00",
      "last_job_started_at": "2026-08-20T17:01:50.118406+00:00",
      "last_error_code": null,
      "last_job_finished_at": "2026-08-20T17:03:28.229920+00:00",
      "last_job_status": "PARTIAL"
    }
  },
  "summary": {
    "stock_count": 2148,
    "latest_score_date": "2026-08-20",
    "status_invariant": true,
    "score_version": "s-only-v2",
    "formula_hash": "d2e96a338ed34348372a0657f7454df9b943b2885a78696e1c4f08c39710f45c"
  },
  "data_status": {
    "dataset_count": 6,
    "job_count": 50,
    "datasets": [
      {
        "dataset": "TaiwanStockHoldingSharesPer",
        "status": "FAILED",
        "latest_source_date": null,
        "attempt_latest_source_date": null,
        "expected_latest_source_date": "2026-08-14",
        "source_age_days": null,
        "rows_received_this_attempt": 0,
        "rows_accepted_this_attempt": 0,
        "rows_rejected_this_attempt": 0,
        "stored_rows_total": 315,
        "staleness": "ERROR",
        "metadata": {
          "requested_start": "2026-07-06",
          "requested_end": "2026-08-20",
          "last_usable_records": 0
        },
        "error_code": "SCHEMA_MISMATCH"
      },
      {
        "dataset": "TaiwanStockInfo",
        "status": "SUCCESS",
        "latest_source_date": "2026-08-20",
        "attempt_latest_source_date": "2026-08-20",
        "expected_latest_source_date": null,
        "source_age_days": null,
        "rows_received_this_attempt": 4307,
        "rows_accepted_this_attempt": 2148,
        "rows_rejected_this_attempt": 2159,
        "stored_rows_total": 2148,
        "staleness": "FRESH",
        "metadata": {
          "universe": {
            "candidate_raw_count": 4307,
            "accepted_common_count": 2148,
            "rejection_counts": {
              "not_supported_common_stock": 1249,
              "duplicate_stock_id": 1171
            },
            "duplicate_stock_ids": 1171,
            "market_counts": {
              "上櫃": 1063,
              "上市": 1995
            },
            "pagination_complete": true,
            "latest_source_date": "2026-08-20"
          }
        },
        "error_code": null
      },
      {
        "dataset": "TaiwanStockInstitutionalInvestorsBuySellWide",
        "status": "FAILED",
        "latest_source_date": "2026-08-20",
        "attempt_latest_source_date": null,
        "expected_latest_source_date": "2026-08-20",
        "source_age_days": 0,
        "rows_received_this_attempt": 0,
        "rows_accepted_this_attempt": 0,
        "rows_rejected_this_attempt": 0,
        "stored_rows_total": 76793,
        "staleness": "ERROR",
        "metadata": {
          "requested_start": "2026-06-24",
          "requested_end": "2026-08-20",
          "last_usable_records": 1892,
          "query_mode": "per_stock_date_range",
          "coverage": {
            "requested": 2148,
            "success": 0,
            "no_data": 0,
            "failed": 6,
            "rows": 0,
            "fatal_code": "QUOTA_EXHAUSTED"
          }
        },
        "error_code": "QUOTA_EXHAUSTED"
      },
      {
        "dataset": "TaiwanStockPrice",
        "status": "FAILED",
        "latest_source_date": "2026-08-20",
        "attempt_latest_source_date": "2026-08-20",
        "expected_latest_source_date": "2026-08-20",
        "source_age_days": 0,
        "rows_received_this_attempt": 41476,
        "rows_accepted_this_attempt": 41476,
        "rows_rejected_this_attempt": 0,
        "stored_rows_total": 43402,
        "staleness": "ERROR",
        "metadata": {
          "requested_start": "2026-06-24",
          "requested_end": "2026-08-20",
          "last_usable_records": 1976,
          "query_mode": "per_stock_date_range",
          "coverage": {
            "requested": 2148,
            "success": 1013,
            "no_data": 110,
            "failed": 116,
            "rows": 41476,
            "fatal_code": "QUOTA_EXHAUSTED"
          }
        },
        "error_code": "QUOTA_EXHAUSTED"
      },
      {
        "dataset": "TaiwanStockShareholding",
        "status": "PARTIAL",
        "latest_source_date": "2026-08-20",
        "attempt_latest_source_date": "2026-08-20",
        "expected_latest_source_date": "2026-08-20",
        "source_age_days": 0,
        "rows_received_this_attempt": 80709,
        "rows_accepted_this_attempt": 80709,
        "rows_rejected_this_attempt": 0,
        "stored_rows_total": 81023,
        "staleness": "FRESH",
        "metadata": {
          "requested_start": "2026-06-24",
          "requested_end": "2026-08-20",
          "last_usable_records": 1974,
          "query_mode": "per_stock_date_range",
          "coverage": {
            "requested": 2148,
            "success": 1980,
            "no_data": 168,
            "failed": 168,
            "rows": 80709,
            "fatal_code": null
          }
        },
        "error_code": "STOCK_PARTIAL"
      },
      {
        "dataset": "TaiwanStockTradingDailyReport",
        "status": "PARTIAL",
        "latest_source_date": null,
        "attempt_latest_source_date": null,
        "expected_latest_source_date": "2026-08-20",
        "source_age_days": null,
        "rows_received_this_attempt": 0,
        "rows_accepted_this_attempt": 0,
        "rows_rejected_this_attempt": 0,
        "stored_rows_total": 327815,
        "staleness": "PARTIAL",
        "metadata": {
          "requested": 2148,
          "skipped_checkpoint": 0,
          "success": 0,
          "failed": 4,
          "rows": 0,
          "retries": 0,
          "query_mode": "per_stock_per_session",
          "retryable_failed": 0,
          "permanent_failed": 0,
          "fatal_code": "QUOTA_EXHAUSTED",
          "stocks_completed": 0,
          "stocks_failed": 1
        },
        "error_code": "QUOTA_EXHAUSTED"
      }
    ]
  },
  "api_timing_ms": {
    "health": 22.39,
    "worker_health": 6.04,
    "summary": 719.58,
    "data_status": 15.94
  },
  "benchmarks": {
    "stocks": {
      "url": "http://192.168.31.138:18080/api/stocks?page=1&page_size=50&sort=score&order=desc",
      "warm": {
        "sample_size": 20,
        "concurrency": 1,
        "median_ms": 65.12,
        "p95_ms": 74.67,
        "p99_ms": 82.86,
        "max_ms": 82.86
      },
      "concurrent": {
        "sample_size": 20,
        "concurrency": 4,
        "median_ms": 130.31,
        "p95_ms": 158.45,
        "p99_ms": 159.43,
        "max_ms": 159.43
      },
      "response_summary": {
        "top_level_keys": [
          "items",
          "page",
          "page_size",
          "total"
        ],
        "item_count": 50,
        "non_null_scores": 3
      }
    },
    "rankings": {
      "url": "http://192.168.31.138:18080/api/rankings?limit=50",
      "warm": {
        "sample_size": 20,
        "concurrency": 1,
        "median_ms": 27.35,
        "p95_ms": 34.06,
        "p99_ms": 34.77,
        "max_ms": 34.77
      },
      "concurrent": {
        "sample_size": 20,
        "concurrency": 4,
        "median_ms": 52.97,
        "p95_ms": 72.39,
        "p99_ms": 79.54,
        "max_ms": 79.54
      },
      "response_summary": {
        "top_level_keys": [
          "items",
          "kind",
          "score_version",
          "source_date"
        ],
        "item_count": 3,
        "non_null_scores": 3
      }
    },
    "detail_2330": {
      "url": "http://192.168.31.138:18080/api/stocks/2330?limit=365",
      "warm": {
        "sample_size": 20,
        "concurrency": 1,
        "median_ms": 344.79,
        "p95_ms": 377.77,
        "p99_ms": 387.16,
        "max_ms": 387.16
      },
      "concurrent": {
        "sample_size": 20,
        "concurrency": 4,
        "median_ms": 1162.0,
        "p95_ms": 1277.88,
        "p99_ms": 1283.11,
        "max_ms": 1283.11
      },
      "response_summary": {
        "top_level_keys": [
          "brokers",
          "calendar_version",
          "foreign_holding",
          "holding_distribution",
          "holding_series",
          "institutional",
          "prices",
          "score",
          "score_history",
          "sources",
          "stock"
        ],
        "source_row_counts": {
          "institutional": 41,
          "foreign_holding": 41,
          "holding_distribution": 105,
          "broker": 29319,
          "price": 41,
          "major_shareholder_5pct": null
        },
        "score_status": "WATCH"
      }
    }
  },
  "row_counts": {
    "universe": 2148,
    "detail_2330_sources": {
      "institutional": 41,
      "foreign_holding": 41,
      "holding_distribution": 105,
      "broker": 29319,
      "price": 41,
      "major_shareholder_5pct": null
    }
  },
  "performance_budgets_ms": {
    "stocks_p95": 500,
    "rankings_p95": 500,
    "detail_p95": 1000
  },
  "sanitized": true,
  "secrets_included": false
}
