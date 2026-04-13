from datetime import datetime

from pydantic import BaseModel, EmailStr, Field, field_validator


class UserCreate(BaseModel):
    email: EmailStr
    display_name: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=8, max_length=128)
    is_admin: bool = False


class RegisterRequest(BaseModel):
    email: EmailStr
    display_name: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=8, max_length=128)


class ProfileUpdate(BaseModel):
    display_name: str | None = Field(None, min_length=1, max_length=120)
    new_password: str | None = Field(None, min_length=8, max_length=128)


class UserOut(BaseModel):
    id: int
    email: str
    display_name: str
    is_admin: bool
    is_active: bool
    avatar_url: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class UserMini(BaseModel):
    id: int
    display_name: str
    avatar_url: str | None = None


class DmConversationOut(BaseModel):
    channel_id: int
    peer_id: int
    peer_display_name: str
    peer_avatar_url: str | None = None
    last_activity_at: datetime | None = None


class DmOpenRequest(BaseModel):
    peer_user_id: int = Field(gt=0)


class ChannelCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    topic: str | None = Field(None, max_length=255)
    is_private: bool = False
    group_id: int | None = None


class ChannelOut(BaseModel):
    id: int
    name: str
    display_label: str
    topic: str | None
    is_private: bool
    created_by_id: int | None = None
    kind: str = "standard"
    created_at: datetime
    group_id: int | None = None
    group_name: str | None = None
    group_created_by_id: int | None = None
    can_delete: bool = False

    model_config = {"from_attributes": True}

    @field_validator("kind", mode="before")
    @classmethod
    def _kind_default(cls, v: str | None) -> str:
        return v if v else "standard"


class ChatGroupCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(None, max_length=2000)


class ChatGroupOut(BaseModel):
    id: int
    name: str
    slug: str
    description: str | None
    created_by_id: int | None
    created_at: datetime

    model_config = {"from_attributes": True}


class ChatGroupMemberAdd(BaseModel):
    user_id: int = Field(gt=0)


class GroupWithChannelsOut(BaseModel):
    group: ChatGroupOut
    channels: list[ChannelOut]
    can_manage: bool = False


class ChatSidebarOut(BaseModel):
    global_channels: list[ChannelOut]
    groups: list[GroupWithChannelsOut]


class MessageCreate(BaseModel):
    body: str = Field(min_length=1, max_length=8000)


class MessageUpdate(BaseModel):
    body: str = Field(min_length=1, max_length=8000)


class MessageOut(BaseModel):
    id: int
    channel_id: int
    user_id: int
    author_name: str
    author_avatar_url: str | None = None
    body: str
    created_at: datetime
    edited_at: datetime | None = None

    model_config = {"from_attributes": True}


class MessageBroadcast(BaseModel):
    type: str = "message"
    message: MessageOut


class MessageDeletedBroadcast(BaseModel):
    type: str = "message_deleted"
    channel_id: int
    message_id: int
