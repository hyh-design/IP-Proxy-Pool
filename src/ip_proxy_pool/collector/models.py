import re
from typing import Annotated, Literal

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, model_validator


class Pagination(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["json_total", "html_max_page"]
    url_template: str = Field(min_length=1)
    total_field: str = "total"
    limit_field: str = "limit"
    page_xpath: str | None = None

    @model_validator(mode="after")
    def validate_template(self) -> "Pagination":
        if "{page}" not in self.url_template:
            raise ValueError("pagination URL template must contain {page}")
        if not self.url_template.startswith("https://"):
            raise ValueError("pagination URL template must use HTTPS")
        if self.kind == "html_max_page" and not self.page_xpath:
            raise ValueError("HTML pagination requires page_xpath")
        return self


class SourceBase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=64)
    region: Literal["domestic", "foreign", "all"]
    urls: tuple[AnyHttpUrl, ...] = Field(min_length=1)
    max_pages: int = Field(20, ge=1, le=100)
    requests_per_second: float = Field(1.0, gt=0, le=10)
    pagination: Pagination | None = None

    @model_validator(mode="after")
    def require_https(self) -> "SourceBase":
        if any(url.scheme != "https" for url in self.urls):
            raise ValueError("source URLs must use HTTPS")
        return self


class RegexSource(SourceBase):
    kind: Literal["regex"] = "regex"
    pattern: str = Field(min_length=1)

    @model_validator(mode="after")
    def compile_pattern(self) -> "RegexSource":
        try:
            re.compile(self.pattern)
        except re.error as error:
            raise ValueError("invalid regular expression") from error
        return self


class JsonSource(SourceBase):
    kind: Literal["json"] = "json"
    list_path: str = Field(min_length=1)
    ip_field: str = Field(min_length=1)
    port_field: str = Field(min_length=1)


class XPathSource(SourceBase):
    kind: Literal["xpath"] = "xpath"
    row_xpath: str = Field(min_length=1)
    ip_xpath: str = Field(min_length=1)
    port_xpath: str = Field(min_length=1)


SourceDefinition = Annotated[
    RegexSource | JsonSource | XPathSource,
    Field(discriminator="kind"),
]
