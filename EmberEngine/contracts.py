"""Integer contracts shared between Ember Deck processes through Synapse.

Only values that more than one worker writes or interprets belong here.
They travel as plain 32-bit ints in shared memory, so changing any number is
a cross-process protocol change: every worker and the UI must be updated and
restarted together.
"""

# /media/active_source: Pawprint's chosen transport target. Aurora and the UI
# use the same value to pick a metadata/artwork namespace.
MEDIA_SOURCE_NONE = 0
MEDIA_SOURCE_PLEX = 1        # /plex/*
MEDIA_SOURCE_BLUETOOTH = 2   # /bt/*
MEDIA_SOURCE_MPRIS = 3       # /mpris/* (local HDMI-2 apps)

# /hdmi2/selected_source: which guest context occupies the HDMI-2 app slot.
HDMI2_SOURCE_PLEX = 1
HDMI2_SOURCE_RADIO = 2       # Shortwave
HDMI2_SOURCE_YOUTUBE = 3     # Pear

# /plex/play_state, /bt/play_state and /mpris/play_state.
PLAY_STOPPED = 0
PLAY_PLAYING = 1
PLAY_PAUSED = 2

# /plex/connection_state: why Plex is or is not available.
PLEX_UNCONFIGURED = 0        # valid config, but no Plex identity/login yet
PLEX_CONNECTING = 1          # an attempt is due or in progress
PLEX_CONNECTED = 2
PLEX_UNAVAILABLE = 3         # server/network failure; retrying with backoff
PLEX_AUTH_FAILED = 4         # credentials rejected; retrying with backoff

# /discord/backend: which Discord bridge runtime the Rich Presence worker chose.
DISCORD_BACKEND_NONE = 0
DISCORD_BACKEND_NATIVE = 1   # bridge built for this CPU (ARM64 on the Deck)
DISCORD_BACKEND_BOX64 = 2    # x86_64 bridge under Box64

# /discord/status: one summary for the panel UI, in rough order of progress.
# Values are stable; 0 is not used.
DISCORD_STATUS_DISABLED = 1
DISCORD_STATUS_NOT_CONFIGURED = 2   # no Discord application ID
DISCORD_STATUS_SDK_UNAVAILABLE = 3  # no usable bridge/SDK/Box64
DISCORD_STATUS_NOT_LINKED = 4
DISCORD_STATUS_AUTHORIZING = 5      # Discord authorization (browser/overlay) pending
DISCORD_STATUS_CONNECTING = 6
DISCORD_STATUS_CONNECTED = 7
DISCORD_STATUS_RECONNECTING = 8
DISCORD_STATUS_ERROR = 9

# /discord/auth_state
DISCORD_AUTH_UNLINKED = 0
DISCORD_AUTH_AUTHORIZING = 1
DISCORD_AUTH_LINKED = 2
DISCORD_AUTH_REFRESHING = 3

# /discord/connection_state
DISCORD_CONNECTION_DISCONNECTED = 0
DISCORD_CONNECTION_CONNECTING = 1
DISCORD_CONNECTION_READY = 2
DISCORD_CONNECTION_RECONNECTING = 3
