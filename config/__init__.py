"""配置层：环境、路径、设置、连接。

对外只需要：

    from config import AppPaths, BotSettings, EnvConfig, PathBridge, load_settings
"""

from .backends import (
    BACKENDS,
    DEFAULT_BACKEND,
    BackendProfile,
    backend_label,
    backend_options,
    get_backend,
    normalize_backend,
)
from .connection import (
    MODE_FORWARD,
    MODE_REVERSE,
    ConnectionSettings,
    normalize_mode,
)
from .environment import (
    EnvConfig,
    EnvKind,
    PathBridge,
    PathMap,
    derive_backend_env,
    derive_profile,
    detect_env,
    env_options,
    load_env_config,
)
from .path_profiles import (
    PROFILES,
    PathProfile,
    get_profile,
    normalize_profile,
    path_profile_options,
)
from .paths import AppPaths
from .settings import BotSettings, load_settings

__all__ = [
    "AppPaths",
    "BotSettings",
    "BACKENDS",
    "BackendProfile",
    "DEFAULT_BACKEND",
    "EnvConfig",
    "EnvKind",
    "PROFILES",
    "PathBridge",
    "PathMap",
    "PathProfile",
    "ConnectionSettings",
    "MODE_FORWARD",
    "MODE_REVERSE",
    "backend_label",
    "backend_options",
    "derive_backend_env",
    "derive_profile",
    "env_options",
    "get_backend",
    "get_profile",
    "normalize_backend",
    "normalize_mode",
    "normalize_profile",
    "path_profile_options",
    "detect_env",
    "load_env_config",
    "load_settings",
]
