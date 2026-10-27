"""Models for ``/v1/email/unsubscribe``: the list an email's link leaves, and whether it is left."""

from typing import Literal

from pydantic import BaseModel, Field

from app.accounts.email_lists import Standing


class EmailSubscription(BaseModel):
    """One mailing list, and where the link's account stands on it."""

    mailing_list: str = Field(
        description=(
            "The list: `alert_rule:<id>` for one alert's emails (the season digest is "
            "one), or `product_email` / `marketing_email`."
        ),
        examples=["alert_rule:42"],
    )
    kind: Literal["alert_rule", "product_email", "marketing_email"] = Field(
        description="What kind of list: one alert's emails, or a kind of email from WhaleWatch.",
        examples=["alert_rule"],
    )
    label: str = Field(
        description="What the list sends, for a sentence: “You won't get emails about …”.",
        examples=["new 13F filings by Berkshire Hathaway"],
    )
    subscribed: bool = Field(
        description="Whether the next email on the list would be sent. After a POST, false.",
        examples=[False],
    )

    @classmethod
    def of(cls, standing: Standing) -> "EmailSubscription":
        return cls(
            mailing_list=standing.mailing_list.id,
            kind=standing.mailing_list.kind.value,
            label=standing.label,
            subscribed=standing.subscribed,
        )
