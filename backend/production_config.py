from typing import Literal
from pydantic import BaseModel, Field, ConfigDict, field_validator
from .core import now

class Preferences(BaseModel):
    model_config = ConfigDict(extra='forbid')
    version: int = 1
    appearance: str = Field('明确成年女性，亚洲面孔优先，性感甜美，自然皮肤，原创角色；发型和脸型适当变化。',max_length=2000)
    wardrobe: str = Field('裙装、黑丝、白丝，结合近期穿搭参考选择，避免完全随机。',max_length=2000)
    framing: str = Field('角色图单人全身，头顶到双脚完整，正常光线；视频近似平视，单人女性，不挡脸，允许全身或半身。',max_length=2000)
    dance_keyword: str = Field('美女单人热舞',min_length=1,max_length=120)
    fashion_keywords: list[str] = Field(default_factory=lambda:['甜妹 裙子 穿搭','黑丝 白丝 裙装'],max_length=6)

class Binding(BaseModel):
    model_config = ConfigDict(extra='forbid')
    app_id: str
    fields: dict[str,str] = Field(default_factory=dict)
    defaults: dict[str,str|int|float|bool] = Field(default_factory=dict)
    schema_hash: str = ''

class ProductionSettings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    image: Binding = Field(default_factory=lambda:Binding(app_id='2065651457367797760'))
    dress: Binding = Field(default_factory=lambda:Binding(app_id='2071308534918246402'))
    motion: Binding = Field(default_factory=lambda:Binding(app_id='2065651453941051392'))
    ds_input_per_million: float = Field(.3,gt=0,le=1000)
    ds_output_per_million: float = Field(1.2,gt=0,le=1000)
    ds_price_checked: str = '2026-10-23'
    ds_price_source: str = 'https://api-docs.deepseek.com/quick_start/pricing/'
    mcp_enabled: bool = False
    mcp_max_count: int = Field(1,ge=1,le=20)
    mcp_ds_budget: float = Field(0,ge=0,le=10000)
    mcp_rh_budget: float = Field(0,ge=0,le=100000)
    mcp_rh_unit: Literal['RH币','USD','CNY'] = 'RH币'

    @field_validator('ds_price_checked')
    @classmethod
    def valid_date(cls,value):
        from datetime import date
        checked=date.fromisoformat(value)
        if checked>date.today():raise ValueError('核对日期不能在未来')
        return value

class ProductionRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: str = Field(min_length=1,max_length=120)
    account: str = 'default'
    count: int = Field(1,ge=1,le=20)
    ds_budget: float = Field(0,ge=0,le=10000)
    rh_budget: float = Field(0,ge=0,le=100000)
    rh_unit: Literal['RH币','USD','CNY'] = 'RH币'
    reference_asset: str = ''
    clothing_asset: str = ''
    duration: int = Field(12,ge=8,le=20)
    notes: str = Field('',max_length=3000)

def preferences(db): return Preferences.model_validate(db.get('production_preferences',{}))
def settings(db): return ProductionSettings.model_validate(db.get('production_settings',{}))
