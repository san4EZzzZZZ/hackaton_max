"""Pydantic schemas for MAX Bot API traffic: inbound updates and outbound payloads."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_TEXT_LENGTH = 4000


class BaseSchema(BaseModel):
    """MAX adds fields over time, so unknown keys are ignored instead of rejected."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore", str_strip_whitespace=True)


# --------------------------------------------------------------------------- inbound


class UserRef(BaseSchema):
    user_id: int
    name: str = ""
    first_name: str = ""
    last_name: str | None = None
    username: str | None = None
    is_bot: bool = False
    last_activity_time: int | None = None

    @property
    def display_name(self) -> str:
        return self.first_name or self.name or (f"@{self.username}" if self.username else "друг")


class MessageBody(BaseSchema):
    mid: str = ""
    seq: int = 0
    text: str | None = None
    attachments: list[dict[str, Any]] = Field(default_factory=list)


class MessageRecipient(BaseSchema):
    chat_id: int | None = None
    chat_type: str = "dialog"
    user_id: int | None = None
    post_id: int | None = None


class Message(BaseSchema):
    sender: UserRef | None = None
    recipient: MessageRecipient = Field(default_factory=MessageRecipient)
    timestamp: int = 0
    body: MessageBody = Field(default_factory=MessageBody)

    @property
    def text(self) -> str:
        return self.body.text or ""

    @property
    def is_from_bot(self) -> bool:
        return bool(self.sender and self.sender.is_bot)


class CallbackPayload(BaseSchema):
    callback_id: str = ""
    timestamp: int = 0
    payload: str | None = None
    user: UserRef | None = None


class Update(BaseSchema):
    """Envelope for every event MAX delivers to a webhook or returns from `/updates`."""

    update_type: str = Field(min_length=1)
    timestamp: int = 0
    chat_id: int | None = None
    user: UserRef | None = None
    message: Message | None = None
    callback: CallbackPayload | None = None
    payload: str | None = None
    is_channel: bool = False
    user_locale: str | None = None

    @property
    def event_user(self) -> UserRef | None:
        if self.callback and self.callback.user:
            return self.callback.user
        if self.user:
            return self.user
        if self.message:
            return self.message.sender
        return None

    @property
    def text(self) -> str:
        return self.message.text if self.message else ""

    @property
    def command(self) -> str | None:
        text = self.text.strip()
        if not text.startswith("/"):
            return None
        name = text[1:].split(maxsplit=1)[0].split("@", 1)[0].lower()
        return name or None

    @property
    def reply_target(self) -> dict[str, int]:
        """Query parameters deciding where the answer goes: dialog -> user_id, chat -> chat_id."""
        recipient = self.message.recipient if self.message else None
        user = self.event_user
        if recipient and recipient.chat_type not in {"dialog", ""} and recipient.chat_id:
            return {"chat_id": recipient.chat_id}
        if self.chat_id and not self.message and not user:
            return {"chat_id": self.chat_id}
        if user:
            return {"user_id": user.user_id}
        if recipient and (recipient.user_id or recipient.chat_id):
            return {"user_id": recipient.user_id} if recipient.user_id else {"chat_id": recipient.chat_id}
        raise ValueError(f"Cannot resolve a reply target for update {self.update_type!r}")

    @property
    def dedupe_key(self) -> str:
        """Scoped per update type: tapping a button on a message is not the message itself."""
        if self.callback and self.callback.callback_id:
            return f"cb:{self.callback.callback_id}"
        if self.message and self.message.body.mid:
            return f"{self.update_type}:msg:{self.message.body.mid}"
        user = self.event_user
        return f"{self.update_type}:{user.user_id if user else 0}:{self.timestamp}"


class UpdatesPage(BaseSchema):
    updates: list[Update] = Field(default_factory=list)
    marker: int | None = None


class SubscriptionInfo(BaseSchema):
    url: str = ""
    time: int = 0
    update_types: list[str] = Field(default_factory=list)


class SubscriptionsList(BaseSchema):
    subscriptions: list[SubscriptionInfo] = Field(default_factory=list)

    @property
    def urls(self) -> list[str]:
        return [item.url for item in self.subscriptions]


# --------------------------------------------------------------------------- outbound


class CallbackButton(BaseSchema):
    type: Literal["callback"] = "callback"
    text: str
    payload: str


class LinkButton(BaseSchema):
    type: Literal["link"] = "link"
    text: str
    url: str


class OpenAppButton(BaseSchema):
    """MAX requires `web_app` here and validates `payload` against `[A-Za-z0-9_-]*` (checked live
    against platform-api2: `start:menu` and URLs are rejected with `proto.payload`)."""

    type: Literal["open_app"] = "open_app"
    text: str
    web_app: str = Field(min_length=1)
    contact_id: int | None = None
    payload: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]*$")


class MessageButton(BaseSchema):
    type: Literal["message"] = "message"
    text: str


class ClipboardButton(BaseSchema):
    type: Literal["clipboard"] = "clipboard"
    text: str
    payload: str


class RequestContactButton(BaseSchema):
    type: Literal["request_contact"] = "request_contact"
    text: str


class RequestGeoLocationButton(BaseSchema):
    type: Literal["request_geo_location"] = "request_geo_location"
    text: str
    quick: bool | None = None


Button = Annotated[
    CallbackButton | LinkButton | OpenAppButton | MessageButton | ClipboardButton
    | RequestContactButton | RequestGeoLocationButton,
    Field(discriminator="type"),
]


class KeyboardPayload(BaseSchema):
    buttons: list[list[Button]]


class InlineKeyboardAttachment(BaseSchema):
    type: Literal["inline_keyboard"] = "inline_keyboard"
    payload: KeyboardPayload


class BotCommand(BaseSchema):
    name: str
    description: str | None = None


class SendMessagePayload(BaseSchema):
    """Body of `POST /messages`."""

    text: str | None = Field(default=None, max_length=MAX_TEXT_LENGTH)
    attachments: list[InlineKeyboardAttachment] = Field(default_factory=list)
    notify: bool = True
    format: Literal["markdown", "html"] | None = None

    @model_validator(mode="after")
    def _single_keyboard(self) -> SendMessagePayload:
        """MAX answers `proto.payload` when a message carries more than one inline keyboard."""
        if len(self.attachments) > 1:
            raise ValueError("A MAX message carries at most one inline keyboard — merge the rows")
        return self

    def as_json(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)


class CallbackAnswerPayload(BaseSchema):
    """Body of `POST /answers?callback_id=...`.

    MAX rejects an empty body with `message: Invalid request. `message` or `notification` required`
    (checked live); `notification` is not in the official SDK types, so only `message` is used here.
    It replaces the message that carried the tapped button, which is what stops its spinner.
    """

    message: SendMessagePayload

    def as_json(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)
