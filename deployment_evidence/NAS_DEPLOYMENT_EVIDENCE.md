# NAS deployment evidence

Sanitized health, summary, data-status and timing responses.

{
  "url": "http://192.168.31.138:18080",
  "health": {
    "status": "ok",
    "service": "api",
    "database": "ok",
    "score_version": "s-only-v1",
    "timezone": "Asia/Taipei"
  },
  "summary": {
    "stock_count": 2148,
    "strong_count": 0,
    "accumulation_count": 0,
    "watch_count": 3,
    "data_insufficient_count": 0,
    "no_strong_evidence_count": 0,
    "latest_score_date": "2026-08-20",
    "last_data_update": "2026-08-20T14:08:16.301193Z",
    "sync_status": [
      {
        "dataset": "TaiwanStockHoldingSharesPer",
        "status": "SUCCESS",
        "latest_source_date": "2026-08-14",
        "last_successful_sync": "2026-08-20T13:50:40.842204Z",
        "records": 315,
        "error_code": null
      },
      {
        "dataset": "TaiwanStockInfo",
        "status": "SUCCESS",
        "latest_source_date": "2026-08-20",
        "last_successful_sync": "2026-08-20T14:08:16.301193Z",
        "records": 2148,
        "error_code": null
      },
      {
        "dataset": "TaiwanStockInstitutionalInvestorsBuySellWide",
        "status": "SUCCESS",
        "latest_source_date": "2026-08-20",
        "last_successful_sync": "2026-08-20T13:50:40.837519Z",
        "records": 1973,
        "error_code": null
      },
      {
        "dataset": "TaiwanStockPrice",
        "status": "SUCCESS",
        "latest_source_date": "2026-08-20",
        "last_successful_sync": "2026-08-20T13:50:40.850859Z",
        "records": 2077,
        "error_code": null
      },
      {
        "dataset": "TaiwanStockShareholding",
        "status": "SUCCESS",
        "latest_source_date": "2026-08-20",
        "last_successful_sync": "2026-08-20T13:50:40.839871Z",
        "records": 2084,
        "error_code": null
      },
      {
        "dataset": "TaiwanStockTradingDailyReport",
        "status": "SUCCESS",
        "latest_source_date": "2026-08-20",
        "last_successful_sync": "2026-08-20T13:50:40.848551Z",
        "records": 87767,
        "error_code": null
      }
    ]
  },
  "data_status": {
    "datasets": [
      {
        "dataset": "TaiwanStockHoldingSharesPer",
        "status": "SUCCESS",
        "latest_source_date": "2026-08-14",
        "last_successful_sync": "2026-08-20T13:50:40.842204Z",
        "records": 315,
        "error_code": null
      },
      {
        "dataset": "TaiwanStockInfo",
        "status": "SUCCESS",
        "latest_source_date": "2026-08-20",
        "last_successful_sync": "2026-08-20T14:08:16.301193Z",
        "records": 2148,
        "error_code": null
      },
      {
        "dataset": "TaiwanStockInstitutionalInvestorsBuySellWide",
        "status": "SUCCESS",
        "latest_source_date": "2026-08-20",
        "last_successful_sync": "2026-08-20T13:50:40.837519Z",
        "records": 1973,
        "error_code": null
      },
      {
        "dataset": "TaiwanStockPrice",
        "status": "SUCCESS",
        "latest_source_date": "2026-08-20",
        "last_successful_sync": "2026-08-20T13:50:40.850859Z",
        "records": 2077,
        "error_code": null
      },
      {
        "dataset": "TaiwanStockShareholding",
        "status": "SUCCESS",
        "latest_source_date": "2026-08-20",
        "last_successful_sync": "2026-08-20T13:50:40.839871Z",
        "records": 2084,
        "error_code": null
      },
      {
        "dataset": "TaiwanStockTradingDailyReport",
        "status": "SUCCESS",
        "latest_source_date": "2026-08-20",
        "last_successful_sync": "2026-08-20T13:50:40.848551Z",
        "records": 87767,
        "error_code": null
      }
    ],
    "jobs": []
  },
  "api_timing_ms": {
    "health": 17.5,
    "summary": 26.13,
    "data_status": 12.5
  },
  "sanitized": true,
  "secrets_included": false
}
