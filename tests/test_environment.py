"""环境 / 路径桥：Windows ↔ WSL 转换与 path_map 覆盖。"""

from pathlib import Path

from config.environment import EnvConfig, EnvKind, PathBridge, PathMap, load_env_config


def _win_wsl(tmp_path):
    env = EnvConfig(bot_env=EnvKind.WINDOWS, napcat_env=EnvKind.WSL)
    return PathBridge(tmp_path, env)


def test_windows_to_wsl_and_back(tmp_path):
    bridge = _win_wsl(tmp_path)
    image = tmp_path / "imgs" / "妈妈.gif"

    wsl = bridge.to_napcat(image)
    assert wsl.startswith("/mnt/")
    assert wsl.endswith("/imgs/妈妈.gif")
    assert bridge.to_bot(wsl) == image.resolve()


def test_relative_paths_are_resolved_against_root(tmp_path):
    bridge = _win_wsl(tmp_path)
    target = bridge.expand("imgs/a.png")
    assert target == (tmp_path / "imgs" / "a.png").resolve()


def test_urls_and_base64_are_passed_through_untouched(tmp_path):
    bridge = _win_wsl(tmp_path)
    for value in (
        "https://example.com/a.png",
        "http://example.com/a.png",
        "base64://AAAA",
        "file:///tmp/a.png",
        "data:image/png;base64,AAAA",
    ):
        assert bridge.to_napcat(value) == value


def test_path_map_wins_over_automatic_conversion(tmp_path):
    env = EnvConfig(
        bot_env=EnvKind.WINDOWS,
        napcat_env=EnvKind.WSL,
        path_map=(PathMap("X:\\share", "/data/share"),),
    )
    bridge = PathBridge(tmp_path, env)

    assert bridge.to_napcat("X:\\share\\a.png") == "/data/share/a.png"
    assert bridge.to_bot("/data/share/a.png") == Path("X:/share/a.png")


def test_env_kind_parse_aliases():
    assert EnvKind.parse("win") is EnvKind.WINDOWS
    assert EnvKind.parse("WSL2") is EnvKind.WSL
    assert EnvKind.parse("posix") is EnvKind.LINUX
    assert EnvKind.parse("", default=EnvKind.WINDOWS) is EnvKind.WINDOWS


def test_load_env_config_keeps_first_source_for_shared_keys():
    env = load_env_config(
        {"bot_env": "windows", "napcat_env": "wsl"},
        {"bot_env": "linux", "wsl_distro": "Debian"},
    )
    assert env.bot_env is EnvKind.WINDOWS
    assert env.napcat_env is EnvKind.WSL
    assert env.wsl_distro == "Debian"


def test_backend_env_is_canonical_and_profile_is_derived(tmp_path):
    env = EnvConfig(bot_env="windows", backend_env="wsl")
    assert env.backend_env is EnvKind.WSL
    assert env.napcat_env is EnvKind.WSL
    assert env.onebot_env is EnvKind.WSL
    assert env.path_profile == "windows_to_wsl"
    # 写盘只写新名字
    data = env.to_dict()
    assert data["backend_env"] == "wsl"
    assert data["path_profile"] == "windows_to_wsl"
    assert "napcat_env" not in data
    assert "onebot_env" not in data

    # 旧构造关键字仍然可用
    legacy = EnvConfig(bot_env="windows", napcat_env="wsl")
    assert legacy.backend_env is EnvKind.WSL
    assert legacy.path_profile == "windows_to_wsl"


def test_profile_can_be_explicit_and_overrides_derivation():
    env = EnvConfig(
        bot_env="windows",
        backend_env="windows",
        path_profile="same",
    )
    assert env.path_profile == "same"
    assert env.translator.to_backend("D:\\Code\\a.png") == "D:\\Code\\a.png"


def test_translator_uses_longest_prefix_and_boundary(tmp_path):
    env = EnvConfig(
        bot_env="windows",
        backend_env="wsl",
        path_map=(PathMap("D:\\share", "/data/share", "NAS"),),
    )
    bridge = PathBridge(tmp_path, env)

    assert bridge.to_backend("D:\\share\\a.png") == "/data/share/a.png"
    assert bridge.to_backend("D:\\Code\\a.png") == "/mnt/d/Code/a.png"
    # D:\share 不能匹配 D:\shareholder
    assert bridge.to_backend("D:\\shareholder\\a.png") == "/mnt/d/shareholder/a.png"
    assert bridge.to_local("/data/share/a.png") == Path("D:/share/a.png")


def test_bridge_reconfigure_keeps_identity_and_applies_new_rules(tmp_path):
    env = EnvConfig(bot_env="windows", backend_env="wsl")
    bridge = PathBridge(tmp_path, env)
    assert bridge.to_backend("D:\\share\\a.png") == "/mnt/d/share/a.png"

    bridge.reconfigure(
        EnvConfig(
            bot_env="windows",
            backend_env="wsl",
            path_map=(PathMap("D:\\share", "/data/share"),),
        )
    )
    assert bridge.to_backend("D:\\share\\a.png") == "/data/share/a.png"


def test_custom_mount_root_roundtrip(tmp_path):
    env = EnvConfig(
        bot_env="windows",
        backend_env="wsl",
        path_profile="windows_to_wsl",
        wsl_mount_root="/windrive",
    )
    bridge = PathBridge(tmp_path, env)

    assert bridge.to_backend("D:\\Code\\a.png") == "/windrive/d/Code/a.png"
    assert bridge.to_local("/windrive/d/Code/a.png") == Path("D:/Code/a.png")
    # 旧配置里的 /windrive/... 也要能还原成本机路径
    assert bridge.expand("/windrive/d/Code/a.png") == Path("D:/Code/a.png")


def test_to_backend_expands_relative_paths(tmp_path):
    bridge = PathBridge(tmp_path, EnvConfig(bot_env="windows", backend_env="wsl"))
    target = bridge.to_backend("imgs/a.png")
    assert target.startswith("/mnt/")
    assert target.endswith("/imgs/a.png")
    # 已经存在的绝对路径直接翻译，不再重复拼 root
    absolute = tmp_path / "imgs" / "a.png"
    assert bridge.to_backend(absolute).endswith("/imgs/a.png")
