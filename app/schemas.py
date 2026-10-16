from datetime import date
from typing import Literal
from pydantic import BaseModel, Field, field_validator, model_validator
from .execution_budget import normalize as normalize_execution_budget


class ExecutionBudgetRequest(BaseModel):
    execution_budget: dict[str, int] | None = None

    @field_validator('execution_budget', mode='before')
    @classmethod
    def check_execution_budget(cls, value):
        return None if value is None else normalize_execution_budget(value)

class RunRequest(ExecutionBudgetRequest):
    ticker: str = Field(default="000333.SZ", pattern=r"^\d{6}\.(SZ|SH|BJ)$")
    year: int = Field(default=2025, ge=2000, le=2100)
    as_of: date = Field(default_factory=date.today)
    question: str = Field(default="收入和利润增长能否获得现金流支持？哪些问题值得进一步研究？", min_length=4, max_length=1500)
    mode: Literal["sample", "live"] = "sample"
    use_model: bool = True
    document_id: str | None = None
    data_source: Literal["default","tushare","eastmoney","documents"] = "default"
    model_profile: str | None = None
    material_ids: list[str] = Field(default_factory=list,max_length=20)

    @model_validator(mode="after")
    def check_time(self):
        if self.year > self.as_of.year:
            raise ValueError("研究年度不能晚于数据截止日")
        if self.as_of > date.today():
            raise ValueError("数据截止日不能晚于今天")
        if self.mode == "sample" and (self.ticker != "000333.SZ" or self.year != 2025):
            raise ValueError("样例回放仅支持美的集团 2025 年；其他公司请用实时研究")
        return self

class ScenarioRequest(BaseModel):
    growth_pct: float = Field(ge=-95, le=300, allow_inf_nan=False)
    pe: float = Field(gt=0, le=150, allow_inf_nan=False)
    shares_100m: float | None = Field(default=None, gt=0, allow_inf_nan=False)

class ReviewRequest(BaseModel):
    text: str = Field(min_length=1, max_length=5000)

class ProfileRequest(BaseModel):
    id: str = Field(min_length=1,max_length=60)
    name: str = Field(min_length=1,max_length=80)
    provider: Literal["deepseek","openai","qwen","anthropic","ollama","custom"]
    protocol: Literal["openai","anthropic"] = "openai"
    base: str = Field(max_length=500)
    model: str = Field(min_length=1,max_length=150)
    json_mode: bool = True
    api_key: str = Field(default="",max_length=4000)
    clear_key: bool = False
    activate: bool = False

class PreferencesRequest(ExecutionBudgetRequest):
    active_model: str | None = None
    data_source: Literal["tushare","eastmoney","documents"] | None = None
    tushare_token: str = Field(default="",max_length=4000)
    clear_tushare: bool = False

class ValuationRequest(BaseModel):
    method: Literal["pe","pb","ps","dcf","ev_ebitda","ddm"]
    assumptions: dict[str,float|str|None] = Field(default_factory=dict)

class ConversationRequest(BaseModel):
    run_id: str | None = None

class TurnRequest(ExecutionBudgetRequest):
    message: str = Field(min_length=1,max_length=8000)
    profile_id: str | None = None
    resume_from_turn_id: str | None = Field(default=None,max_length=80)

class ReportRequest(BaseModel):
    ticker: str = Field(pattern=r"^\d{6}\.(SZ|SH|BJ)$")
    year: int = Field(ge=2000,le=2100)
    as_of: date = Field(default_factory=date.today)
