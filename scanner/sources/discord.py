"""Discord *reading* - intentionally not implemented.

v1 only sends alerts to Discord via a webhook (see alerts.py). Reading
servers must be done by a proper bot account that a server admin has invited,
never by automating a user account (self-bots violate Discord's terms).
This stub marks where such a bot would plug in.
"""


class DiscordMentionSource:
    def __init__(self, *args, **kwargs):
        raise NotImplementedError("Discord reading is not part of v1.")
