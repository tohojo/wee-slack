from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Generic, Optional, TypeVar, Union, cast

import weechat

from slack.shared import shared
from slack.util import get_callback_name

if TYPE_CHECKING:
    from typing_extensions import Literal


class WeeChatColor(str):
    pass


@dataclass
class WeeChatConfig:
    name: str

    def __post_init__(self):
        self.pointer = weechat.config_new(self.name, "", "")


@dataclass
class WeeChatSection:
    weechat_config: WeeChatConfig
    name: str
    user_can_add_options: bool = False
    user_can_delete_options: bool = False
    callback_read: str = ""
    callback_write: str = ""

    def __post_init__(self):
        self.pointer = weechat.config_new_section(
            self.weechat_config.pointer,
            self.name,
            self.user_can_add_options,
            self.user_can_delete_options,
            self.callback_read,
            "",
            self.callback_write,
            "",
            "",
            "",
            "",
            "",
            "",
            "",
        )


WeeChatOptionTypes = Union[int, str]
WeeChatOptionType = TypeVar("WeeChatOptionType", bound=WeeChatOptionTypes)


def option_get_value(
    option_pointer: str, option_type: WeeChatOptionType
) -> WeeChatOptionType:
    if isinstance(option_type, bool):
        return cast(WeeChatOptionType, weechat.config_boolean(option_pointer) == 1)
    if isinstance(option_type, int):
        return cast(WeeChatOptionType, weechat.config_integer(option_pointer))
    if isinstance(option_type, WeeChatColor):
        color = weechat.config_color(option_pointer)
        return cast(WeeChatOptionType, WeeChatColor(color))
    return cast(WeeChatOptionType, weechat.config_string(option_pointer))


@dataclass
class WeeChatOption(Generic[WeeChatOptionType]):
    section: WeeChatSection
    name: str
    description: str
    default_value: WeeChatOptionType
    min_value: Optional[int] = None
    max_value: Optional[int] = None
    string_values: Optional[list[WeeChatOptionType]] = None
    parent_option: Union[WeeChatOption[WeeChatOptionType], str, None] = None
    callback_change: Optional[
        Callable[[WeeChatOption[WeeChatOptionType], bool], None]
    ] = None

    def __post_init__(self):
        self._pointer = self._create_weechat_option()

    def __bool__(self) -> bool:
        return bool(self.value)

    @property
    def value(self) -> WeeChatOptionType:
        if weechat.config_option_is_null(self._pointer):
            if isinstance(self.parent_option, str):
                parent_option_pointer = weechat.config_get(self.parent_option)
                return option_get_value(parent_option_pointer, self.default_value)
            elif self.parent_option is not None:
                return self.parent_option.value
            return self.default_value
        return option_get_value(self._pointer, self.default_value)

    @value.setter
    def value(self, value: WeeChatOptionType):
        rc = self.value_set_as_str(str(value))
        if rc == weechat.WEECHAT_CONFIG_OPTION_SET_ERROR:
            raise Exception(f"Failed to value for option: {self.name}")

    def value_set_as_str(self, value: str) -> int:
        return weechat.config_option_set(self._pointer, value, 1)

    def value_set_null(self) -> int:
        if self.parent_option is None:
            raise Exception(
                f"Can't set null value for option without parent: {self.name}"
            )
        return weechat.config_option_set_null(self._pointer, 1)

    @property
    def weechat_type(
        self,
    ) -> Literal["integer", "boolean", "color", "string"]:
        if self.string_values:
            return "integer"
        if isinstance(self.default_value, bool):
            return "boolean"
        if isinstance(self.default_value, int):
            return "integer"
        if isinstance(self.default_value, WeeChatColor):
            return "color"
        return "string"

    def _changed_cb(self, data: str, option: str, value: Optional[str] = None):
        if self.callback_change:
            parent_changed = data == "parent_changed"
            if not parent_changed or weechat.config_option_is_null(self._pointer):
                self.callback_change(self, parent_changed)
        return weechat.WEECHAT_RC_OK

    def _create_weechat_option(self) -> str:
        if self.parent_option is not None:
            if isinstance(self.parent_option, str):
                parent_option_name = self.parent_option
                name = f"{self.name} << {parent_option_name}"
            else:
                parent_option_name = (
                    f"{self.parent_option.section.weechat_config.name}"
                    f".{self.parent_option.section.name}"
                    f".{self.parent_option.name}"
                )
                name = f"{self.name} << {parent_option_name}"
            default_value = None
            null_value_allowed = True
            weechat.hook_config(
                parent_option_name,
                get_callback_name(self._changed_cb),
                "parent_changed",
            )
        else:
            name = self.name
            default_value = (
                str(self.default_value).lower()
                if self.weechat_type == "boolean"
                else str(self.default_value)
            )
            null_value_allowed = False

        value = None

        if shared.weechat_version < 0x03050000:
            default_value = str(default_value)
            value = default_value

        return weechat.config_new_option(
            self.section.weechat_config.pointer,
            self.section.pointer,
            name,
            self.weechat_type,
            self.description,
            "|".join(str(x) for x in self.string_values or []),
            self.min_value or -(2**31),
            self.max_value or 2**31 - 1,
            default_value,
            value,
            null_value_allowed,
            "",
            "",
            get_callback_name(self._changed_cb),
            "",
            "",
            "",
        )
