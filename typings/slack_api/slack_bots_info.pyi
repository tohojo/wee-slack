from typing import Dict, Literal, TypedDict, final

from typing_extensions import NotRequired

class SlackBotInfo(TypedDict):
    id: str
    deleted: bool
    name: str
    updated: int
    app_id: str
    user_id: NotRequired[str]
    icons: Dict[str, str]

@final
class SlackBotsInfoErrorResponse(TypedDict):
    ok: Literal[False]
    error: str

@final
class SlackBotsInfoSuccessResponse(TypedDict):
    ok: Literal[True]
    bot: SlackBotInfo

SlackBotInfoResponse = SlackBotsInfoSuccessResponse | SlackBotsInfoErrorResponse
