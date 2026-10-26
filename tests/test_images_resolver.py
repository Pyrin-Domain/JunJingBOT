"""images 模块：占位符展开、远程 / base64 / 本地路径解析。"""

from pathlib import Path

from config.environment import EnvConfig, EnvKind, PathBridge
from config.paths import AppPaths
from images.models import ImageKind
from images.resolver import ImageResolver


def _resolver(tmp_path):
    paths = AppPaths.discover(tmp_path)
    bridge = PathBridge(
        tmp_path, EnvConfig(bot_env=EnvKind.WINDOWS, napcat_env=EnvKind.WSL)
    )
    return ImageResolver(paths, bridge, images_dir=tmp_path / "imgs"), bridge


def test_expand_replaces_project_placeholders(tmp_path):
    resolver, _ = _resolver(tmp_path)
    text = resolver.expand("{imgs_dir}/a.png")
    assert Path(text) == tmp_path / "imgs" / "a.png"
    assert str(tmp_path) in resolver.expand("{root}/x")
    assert str(tmp_path / "data" / "downloads") in resolver.expand(
        "{downloads_dir}/v.mp4"
    )


def test_remote_and_base64_are_not_touched(tmp_path):
    resolver, _ = _resolver(tmp_path)

    remote = resolver.resolve("https://example.com/a.png")
    assert remote.kind is ImageKind.REMOTE
    assert remote.napcat_path == "https://example.com/a.png"

    encoded = resolver.resolve("base64://AAAA")
    assert encoded.kind is ImageKind.BASE64
    assert encoded.napcat_path == "base64://AAAA"


def test_local_image_uses_path_bridge_for_napcat_path(tmp_path):
    resolver, bridge = _resolver(tmp_path)
    image = tmp_path / "imgs" / "妈妈.gif"
    image.parent.mkdir(parents=True, exist_ok=True)
    image.write_bytes(b"gif")

    ref = resolver.resolve("{imgs_dir}/妈妈.gif")

    assert ref.kind is ImageKind.LOCAL
    assert ref.bot_path == image.resolve()
    assert ref.napcat_path == bridge.to_napcat(image)
    assert ref.backend_path == ref.napcat_path
    assert ref.napcat_path.startswith("/mnt/")
    assert resolver.to_backend(image) == ref.napcat_path
    assert resolver.to_napcat(image) == ref.napcat_path
    assert resolver.exists("{imgs_dir}/妈妈.gif") is True


def test_missing_local_file_is_still_resolvable(tmp_path):
    resolver, _ = _resolver(tmp_path)
    ref = resolver.resolve("imgs/not-there.png")
    assert ref.bot_path == (tmp_path / "imgs" / "not-there.png").resolve()
    assert resolver.exists("imgs/not-there.png") is False
