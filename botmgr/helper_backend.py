"""Standard execution backend: drives bots through the privileged helper.

Implements the Backend protocol (botmgr.backend). The helper's operations
are addressed by bot name (it owns the tmux socket naming), so this backend
remembers which bot each started identity belongs to.
"""

from __future__ import annotations

from botmgr.helper import HelperClient
from botmgr.launcher import LaunchConfig
from botmgr.procident import ProcessIdentity


class HelperBackend:
    def __init__(self, client: HelperClient) -> None:
        self._client = client
        self._owners: dict[tuple[int, int], str] = {}

    def start(self, bot_id: str, config: LaunchConfig) -> ProcessIdentity:
        identity = self._client.start_bot(bot_id, config)
        self._owners[(identity.pid, identity.start_time)] = bot_id
        return identity

    def _bot_for(self, identity: ProcessIdentity) -> str:
        try:
            return self._owners[(identity.pid, identity.start_time)]
        except KeyError:
            raise KeyError(
                f"identity not started by this backend: pid {identity.pid}"
            ) from None

    def stop(self, identity: ProcessIdentity, grace_seconds: float) -> int:
        bot_id = self._bot_for(identity)
        exit_code = self._client.stop_bot(bot_id, identity, grace_seconds)
        del self._owners[(identity.pid, identity.start_time)]
        return exit_code

    def poll_exit(self, identity: ProcessIdentity) -> int | None:
        bot_id = self._bot_for(identity)
        alive, exit_code = self._client.bot_status(bot_id, identity)
        if alive:
            return None
        del self._owners[(identity.pid, identity.start_time)]
        return exit_code if exit_code is not None else -1
