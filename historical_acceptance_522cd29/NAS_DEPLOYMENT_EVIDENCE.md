# NAS deployment evidence

Sanitized health, summary, data-status and timing responses.

{
  "url": "http://192.168.31.138:18080",
  "health": {
    "status": "ok",
    "service": "api",
    "database": "ok",
    "score_version": "s-only-v4",
    "formula_hash": "6996b96ab431d160cd20c29bee6bb1d0ba04be519d024be8a319e03817a47bb8",
    "timezone": "Asia/Taipei"
  },
  "worker_health": {
    "status": "ok",
    "ready": true,
    "heartbeat_age_seconds": 0,
    "scheduler_age_seconds": 0,
    "progress_age_seconds": 0,
    "progress_deadline_seconds": 180,
    "stale": false,
    "prolonged_job": false,
    "scheduler_ready": true,
    "job_progress_active": false,
    "scheduler_contract_missing": false,
    "heartbeat": {
      "status": "idle",
      "ready": true,
      "scheduler_started_at": "2026-08-21T05:40:23.115848+00:00",
      "last_heartbeat_at": "2026-08-21T05:43:39.463219+00:00",
      "last_job_started_at": "2026-08-21T05:36:39.234540+00:00",
      "last_error_code": "QUOTA_EXHAUSTED",
      "last_job_finished_at": "2026-08-21T05:40:23.114169+00:00",
      "last_job_status": "PARTIAL",
      "scheduler_ready": true,
      "last_scheduler_heartbeat_at": "2026-08-21T05:43:39.462867+00:00",
      "last_job_progress_at": "2026-08-21T05:40:23.002617+00:00",
      "job_phase": "TaiwanStockInstitutionalInvestorsBuySellWide completed=868/2148",
      "next_expected_run_at": "2026-08-21T13:30:00+00:00"
    },
    "age_seconds": 0,
    "overdue": false
  },
  "summary": {
    "stock_count": 2148,
    "latest_score_date": null,
    "status_invariant": true,
    "score_version": "s-only-v4",
    "formula_hash": "6996b96ab431d160cd20c29bee6bb1d0ba04be519d024be8a319e03817a47bb8"
  },
  "data_status": {
    "dataset_count": 6,
    "job_count": 50,
    "datasets": [
      {
        "dataset": "TaiwanStockHoldingSharesPer",
        "status": "FAILED",
        "latest_source_date": "2026-08-14",
        "attempt_latest_source_date": null,
        "expected_latest_source_date": "2026-08-14",
        "source_age_days": 0,
        "rows_received_this_attempt": 0,
        "rows_accepted_this_attempt": 19995,
        "rows_rejected_this_attempt": 0,
        "stored_rows_total": 191670,
        "staleness": "ERROR",
        "metadata": {
          "requested_start": "2026-06-24",
          "requested_end": "2026-08-20",
          "last_usable_records": 0,
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
        "error_code": "e3q8"
      },
      {
        "dataset": "TaiwanStockInfo",
        "status": "SUCCESS",
        "latest_source_date": "2026-08-21",
        "attempt_latest_source_date": "2026-08-21",
        "expected_latest_source_date": "2026-08-20",
        "source_age_days": -1,
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
              "invalid_identifier": 591,
              "unsupported_market": 383,
              "etf": 14
            },
            "duplicate_stock_ids": 1171,
            "market_counts": {
              "上櫃": 924,
              "上市": 1224
            },
            "pagination_complete": true,
            "latest_source_date": "2026-08-21",
            "reconciliation": {
              "raw_count": 4307,
              "duplicate_count": 1171,
              "rejected_unique_count": 988,
              "accepted_common_count": 2148,
              "reconciles": true
            }
          }
        },
        "error_code": null
      },
      {
        "dataset": "TaiwanStockInstitutionalInvestorsBuySellWide",
        "status": "FAILED",
        "latest_source_date": "2026-08-20",
        "attempt_latest_source_date": "2026-08-20",
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
            "newly_fetched": 0,
            "reused_complete": 0,
            "reused_valid_no_data": 0,
            "valid_no_data": 0,
            "retryable_pending": 2148,
            "permanent_failed": 0,
            "physical_requests": 868,
            "failed": 868,
            "rows": 0,
            "fatal_code": "QUOTA_EXHAUSTED",
            "checkpoint_state": "resumed",
            "selection_policy": "sorted_stock_id_observation_resume",
            "observation_cadence": "trading_session",
            "expected_observations_per_stock": 42,
            "verified_observations": 76793,
            "unresolved_observations": 13423,
            "partial_responses": 0
          }
        },
        "error_code": "QUOTA_EXHAUSTED"
      },
      {
        "dataset": "TaiwanStockPrice",
        "status": "FAILED",
        "latest_source_date": "2026-08-20",
        "attempt_latest_source_date": null,
        "expected_latest_source_date": "2026-08-20",
        "source_age_days": 0,
        "rows_received_this_attempt": 0,
        "rows_accepted_this_attempt": 0,
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
        "error_code": "7s2a"
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
        "staleness": "PARTIAL",
        "metadata": {
          "requested_start": "2026-06-24",
          "requested_end": "2026-08-20",
          "last_usable_records": 1974,
          "query_mode": "per_stock_date_range",
          "coverage": {
            "requested": 2148,
            "success": 0,
            "newly_fetched": 0,
            "reused_complete": 0,
            "reused_valid_no_data": 0,
            "valid_no_data": 0,
            "retryable_pending": 2148,
            "permanent_failed": 0,
            "physical_requests": 2148,
            "failed": 2148,
            "rows": 80709,
            "fatal_code": null,
            "checkpoint_state": "new",
            "selection_policy": "sorted_stock_id_observation_resume",
            "observation_cadence": "trading_session",
            "expected_observations_per_stock": 42,
            "verified_observations": 80709,
            "unresolved_observations": 9507,
            "partial_responses": 1980
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
    "health": 41.04,
    "worker_health": 5.22,
    "summary": 15.29,
    "data_status": 19.5
  },
  "benchmarks": {
    "stocks": {
      "url": "http://192.168.31.138:18080/api/stocks?page=1&page_size=50&sort=score&order=desc",
      "warm": {
        "sample_size": 20,
        "concurrency": 1,
        "median_ms": 14.81,
        "p95_ms": 22.64,
        "p99_ms": 30.02,
        "max_ms": 30.02
      },
      "concurrent": {
        "sample_size": 20,
        "concurrency": 4,
        "median_ms": 28.36,
        "p95_ms": 67.06,
        "p99_ms": 69.98,
        "max_ms": 69.98
      },
      "response_summary": {
        "top_level_keys": [
          "items",
          "page",
          "page_size",
          "total"
        ],
        "item_count": 50,
        "non_null_scores": 0
      }
    },
    "rankings": {
      "url": "http://192.168.31.138:18080/api/rankings?limit=50",
      "warm": {
        "sample_size": 20,
        "concurrency": 1,
        "median_ms": 6.53,
        "p95_ms": 8.05,
        "p99_ms": 8.54,
        "max_ms": 8.54
      },
      "concurrent": {
        "sample_size": 20,
        "concurrency": 4,
        "median_ms": 20.75,
        "p95_ms": 39.43,
        "p99_ms": 39.83,
        "max_ms": 39.83
      },
      "response_summary": {
        "top_level_keys": [
          "items",
          "provider_state",
          "score_version",
          "source_date"
        ],
        "item_count": 0,
        "non_null_scores": 0
      }
    },
    "detail_2330": {
      "url": "http://192.168.31.138:18080/api/stocks/2330?limit=365",
      "warm": {
        "sample_size": 20,
        "concurrency": 1,
        "median_ms": 353.14,
        "p95_ms": 415.59,
        "p99_ms": 430.44,
        "max_ms": 430.44
      },
      "concurrent": {
        "sample_size": 20,
        "concurrency": 4,
        "median_ms": 1109.48,
        "p95_ms": 1294.28,
        "p99_ms": 1307.26,
        "max_ms": 1307.26
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
          "holding_distribution": 120,
          "broker": 29319,
          "price": 41,
          "major_shareholder_5pct": null
        },
        "score_status": "DATA_INSUFFICIENT"
      }
    }
  },
  "row_counts": {
    "universe": 2148,
    "detail_2330_sources": {
      "institutional": 41,
      "foreign_holding": 41,
      "holding_distribution": 120,
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
