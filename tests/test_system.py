import subprocess
import zipfile

import pytest

from app import system


def git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin:/opt/homebrew/bin"})


@pytest.fixture
def repos(tmp_path, monkeypatch):
    """원격(origin) 저장소 + 설치된 복사본. 원격에만 새 커밋을 올려 업데이트를 흉내낸다."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    dev = tmp_path / "dev"
    subprocess.run(["git", "clone", "-q", str(origin), str(dev)], check=True, capture_output=True)
    (dev / "a.txt").write_text("1")
    git(dev, "add", ".")
    git(dev, "commit", "-qm", "첫 버전")
    git(dev, "push", "-q", "origin", "HEAD:main")
    installed = tmp_path / "installed"
    subprocess.run(["git", "clone", "-q", str(origin), str(installed)], check=True, capture_output=True)
    monkeypatch.setattr(system, "REPO_DIR", installed)
    monkeypatch.setattr(system, "_find_uv", lambda: None)
    return dev, installed


def test_version(repos):
    v = system.version()
    assert v["subject"] == "첫 버전" and len(v["commit"]) >= 7


def test_check_and_apply_update(repos):
    dev, installed = repos
    assert system.check_update()["available"] is False

    (dev / "a.txt").write_text("2")
    git(dev, "commit", "-qam", "화자 이름 기능 개선")
    git(dev, "push", "-q", "origin", "HEAD:main")

    r = system.check_update()
    assert r["available"] is True and r["changes"] == ["화자 이름 기능 개선"]
    result = system.apply_update()
    assert result["ok"] and result["version"]["subject"] == "화자 이름 기능 개선"
    assert (installed / "a.txt").read_text() == "2"


def test_check_update_offline(repos, monkeypatch):
    _, installed = repos
    git(installed, "remote", "set-url", "origin", "/nonexistent/repo.git")
    r = system.check_update()
    assert r["available"] is None and "인터넷" in r["message"]


def test_export_logs_has_no_transcripts(tmp_path):
    data = tmp_path / "data"
    (data / "logs").mkdir(parents=True)
    (data / "logs" / "kkachi.log").write_text("hello log")
    (data / "recordings" / "abc").mkdir(parents=True)
    (data / "recordings" / "abc" / "original.wav").write_bytes(b"secret audio")
    path = system.export_logs(data, {"done": 3}, dest_dir=tmp_path / "desk")
    names = zipfile.ZipFile(path).namelist()
    assert sorted(names) == ["logs/kkachi.log", "기기정보.txt"]
    info = zipfile.ZipFile(path).read("기기정보.txt").decode()
    assert "macOS" in info and "{'done': 3}" in info
