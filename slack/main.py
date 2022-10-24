from __future__ import annotations

import weechat

from . import globals as G
from .config import SlackConfig, SlackWorkspace
from .task import create_task
from .util import get_callback_name


def shutdown_cb():
    weechat.config_write(G.config.weechat_config.pointer)
    return weechat.WEECHAT_RC_OK


async def init():
    print(G.workspaces)
    if "wee-slack-test" not in G.workspaces:
        G.workspaces["wee-slack-test"] = SlackWorkspace("wee-slack-test")
        G.workspaces[
            "wee-slack-test"
        ].config.api_token.value = weechat.config_get_plugin("api_token")
        G.workspaces[
            "wee-slack-test"
        ].config.api_cookies.value = weechat.config_get_plugin("api_cookie")
    workspace = G.workspaces["wee-slack-test"]
    print(workspace)
    print(workspace.config.slack_timeout.value)
    print(G.config.color.reaction_suffix.value)


def main():
    if weechat.register(
        G.SCRIPT_NAME,
        G.SCRIPT_AUTHOR,
        G.SCRIPT_VERSION,
        G.SCRIPT_LICENSE,
        G.SCRIPT_DESC,
        get_callback_name(shutdown_cb),
        "",
    ):
        G.weechat_version = int(weechat.info_get("version_number", "") or 0)
        G.workspaces = {}
        G.config = SlackConfig()
        G.config.config_read()
        create_task(init(), final=True)
