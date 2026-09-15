from datetime import date

from pydantic import BaseModel, ConfigDict, model_validator

from . import clock


class APIModel(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)


class DatedConditionsIn(APIModel):
    effective_date: date | None = None

    @model_validator(mode='after')
    def validate_effective_date(self):
        if self.effective_date and self.effective_date > clock.today():
            raise ValueError('Дата начала действия не может быть в будущем')
        return self
