from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class StockListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    stock_id: str
    stock_name: str
    market: str
    industry: str | None = None
    is_favorite: bool = False
    price: float | None = None
    price_change: float | None = None
    score: float | None = None
    status: str = "DATA_INSUFFICIENT"
    score_version: str | None = None
    features: dict[str, Any] = Field(default_factory=dict)
    coverage: dict[str, Any] = Field(default_factory=dict)
    latest_data: str | None = None
    data_status: str = "NO_DATA"
    data_latest_source_date: str | None = None
    last_updated_at: Any | None = None
    data_sources: dict[str, Any] = Field(default_factory=dict)
    refresh_issue: dict[str, Any] | None = None


class PaginatedStocks(BaseModel):
    items: list[StockListItem]
    total: int
    page: int
    page_size: int
