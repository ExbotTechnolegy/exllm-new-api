#!/usr/bin/env python3
"""Install a verified product release into an existing EXAI Compose deployment."""

import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import uuid


IMAGE_ARCHIVE = "new-api-image-linux-amd64.tar.gz"
ARCHIVES = {IMAGE_ARCHIVE, "new-api-linux-amd64.tar.gz", "exllm-site.tar.gz"}
PACKAGE_LIMITS = {name: 512 * 1024**2 for name in ARCHIVES}
PACKAGE_LIMITS.update({"BUILD-METADATA": 4096, "SHA256SUMS": 4096})
SITE_PATHS = {"landing", "models", "docs", "logo.svg", "favicon.ico", "exbot-auth-v3.css"}
SITE_PAGES = {"/": "landing/public.html", "/models/": "models/index.html", "/docs/": "docs/index.html"}
PRIVATE_CONFIG = "/etc/exai-release.json"
COMMIT = re.compile(r"[0-9a-f]{40}")
VERSION = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._+-]{0,79}")


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def command(args, label, timeout=120):
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    require(result.returncode == 0, f"{label} failed (exit {result.returncode})")
    return result.stdout


def atomic_write(path, data, mode=0o600):
    fd, temp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as file:
            os.fchmod(file.fileno(), mode)
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp, path)
    finally:
        Path(temp).unlink(missing_ok=True)


def unpack_package(stream, dest, sha):
    received = set()
    with tarfile.open(fileobj=stream, mode="r|") as archive:
        for member in archive:
            name = member.name
            require(name in PACKAGE_LIMITS and name not in received
                    and member.isfile() and 0 < member.size <= PACKAGE_LIMITS[name],
                    "Invalid release package member")
            with archive.extractfile(member) as src, (dest / name).open("xb") as target:
                shutil.copyfileobj(src, target)
            received.add(name)
    require(received == PACKAGE_LIMITS.keys(), "Incomplete release package")
    metadata = dict(line.split("=", 1) for line in (dest / "BUILD-METADATA").read_text().splitlines())
    version = metadata.get("application_version", "")
    require(metadata.get("source_commit") == sha and metadata.get("platform") == "linux/amd64"
            and VERSION.fullmatch(version), "Invalid build metadata")
    checksums = {}
    for line in (dest / "SHA256SUMS").read_text().splitlines():
        digest, name = line.split()
        require(name in ARCHIVES and name not in checksums and re.fullmatch(r"[0-9a-f]{64}", digest),
                "Invalid checksum manifest")
        checksums[name] = digest
    require(checksums.keys() == ARCHIVES, "Incomplete checksum manifest")
    for name, expected in checksums.items():
        digest = hashlib.sha256()
        with (dest / name).open("rb") as file:
            for block in iter(lambda: file.read(1024**2), b""):
                digest.update(block)
        require(digest.hexdigest() == expected, "Product archive checksum mismatch")
    return version


def stage_site(archive_path, current_site, target):
    with tarfile.open(archive_path, mode="r:gz") as archive:
        paths = set()
        roots = set()
        size = 0
        for member in archive:
            path = PurePosixPath(member.name)
            if path.parts == ("exllm-site",) and member.isdir():
                continue
            require(len(path.parts) >= 2 and path.parts[0] == "exllm-site"
                    and path.parts[1] in SITE_PATHS and ".." not in path.parts and path not in paths
                    and (member.isdir() or member.isfile()), "Invalid static archive member")
            paths.add(path)
            roots.add(path.parts[1])
            size += member.size
            require(len(paths) <= 30000 and size <= 512 * 1024**2, "Static archive exceeds size limit")
        require({"landing", "models"} <= roots, "Static package is missing public pages")
        shutil.copytree(current_site, target, symlinks=True,
                        ignore=lambda folder, _: roots if Path(folder) == current_site else set())
        for member in archive:
            parts = PurePosixPath(member.name).parts[1:]
            if not parts:
                continue
            path = target.joinpath(*parts)
            if member.isdir():
                path.mkdir(parents=True, exist_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as src, path.open("xb") as out:
                    shutil.copyfileobj(src, out)
                path.chmod(0o644)
        require(all((target / path).is_file() for path in SITE_PAGES.values()),
                "Static release is missing a required page")


class Release:
    def __init__(self, config):
        self.site_root = Path(config["site_root"]).resolve(strict=True)
        self.releases = (self.site_root / "releases").resolve(strict=True)
        self.pointer = self.site_root / "current"
        self.private = Path(config["state_dir"]).resolve(strict=True)
        self.state_path = self.private / "state.json"
        self.runtime_env = Path(config["compose_env"]).resolve(strict=True)
        self.compose = ["docker", "compose", "-p", config["compose_project"], "--env-file",
                        str(self.runtime_env), "-f", config["compose_file"]]
        self.host = config["origin_host"]
        require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", self.host), "Invalid origin hostname")

    def current_site(self):
        require(self.pointer.is_symlink(), "The site's current pointer must be a symlink")
        return self.site_path(self.pointer.resolve(strict=True).name, self.pointer)

    def site_path(self, name, pointer=None):
        path = (self.releases / name).resolve(strict=True)
        require(path.parent == self.releases and path.is_dir()
                and (pointer is None or pointer.resolve() == path), "Invalid static release path")
        return path

    def request(self, path):
        return command(["curl", "--noproxy", "*", "-fsS", "--max-time", "10",
                        "--resolve", f"{self.host}:443:127.0.0.1", f"https://{self.host}{path}"],
                       f"Origin check for {path}")

    def verify(self, release):
        site = self.site_path(release["site"])
        status = json.loads(self.request("/api/status"))
        require(status.get("success") is True and status.get("data", {}).get("version") == release["version"],
                "Unexpected production application version")
        for route, file in SITE_PAGES.items():
            require(self.request(route) == (site / file).read_bytes(), f"Static content mismatch on {route}")
        for route in ("/sign-in", "/keys"):
            require(b"<html" in self.request(route).lower(), f"Expected console HTML at {route}")

    def running_image(self):
        container = command(self.compose + ["ps", "-q", "new-api"], "Find the New API container").decode().strip()
        require(re.fullmatch(r"[0-9a-f]{12,64}", container), "Expected one running New API container")
        image_id, ref = command(["docker", "inspect", "--format", "{{.Image}} {{.Config.Image}}", container],
                                "Inspect the New API container").decode().split()
        return image_id, ref

    def write_state(self, state):
        atomic_write(self.state_path, (json.dumps(state, indent=2) + "\n").encode())

    def bootstrap(self, sha):
        require(not self.state_path.exists(), "Release state has already been initialized")
        image_id, ref = self.running_image()
        image = json.loads(command(["docker", "image", "inspect", image_id], "Inspect baseline image"))[0]
        labels = image["Config"].get("Labels") or {}
        require(labels.get("org.opencontainers.image.revision") == sha or ref.endswith(":" + sha),
                "Baseline commit does not match the local image")
        version = json.loads(self.request("/api/status"))["data"]["version"]
        require(VERSION.fullmatch(version), "Invalid baseline version")
        baseline = {"commit": sha, "image": ref, "image_id": image_id, "version": version,
                    "site": self.current_site().name}
        self.verify(baseline)
        self.write_state({"current": baseline, "previous": None})
        print(f"Recorded baseline {sha} ({version})", flush=True)

    def activate(self, release):
        site = self.site_path(release["site"])
        info = command(["docker", "image", "inspect", "--format", "{{.Id}}", release["image"]],
                       "Check release image").decode().strip()
        require(info == release["image_id"], "Release image changed or is unavailable")
        env = self.runtime_env.read_bytes()
        updated, count = re.subn(rb"(?m)^EXAI_IMAGE=[^\r\n]*",
                                 b"EXAI_IMAGE=" + release["image"].encode(), env)
        require(count == 1, "Expected one EXAI_IMAGE setting in the Compose environment")
        if env != updated:
            atomic_write(self.runtime_env, updated, self.runtime_env.stat().st_mode & 0o777)
        command(self.compose + ["up", "-d", "--no-deps", "--no-build", "--pull", "never", "--wait",
                                "--wait-timeout", "90", "new-api"], "Activate New API")
        require(self.running_image()[0] == release["image_id"], "New API started with an unexpected image")
        with tempfile.TemporaryDirectory(dir=self.site_root) as tmp:
            link = Path(tmp) / "current"
            link.symlink_to(site)
            os.replace(link, self.pointer)
        self.verify(release)

    def prepare(self, sha, state):
        image = f"exllm-release:{sha}"
        with tempfile.TemporaryDirectory(dir=self.private) as tmp:
            package = Path(tmp)
            version = unpack_package(sys.stdin.buffer, package, sha)
            require(all(item is None or item["image"] != image
                        for item in (state["current"], state["previous"])), "Commit is already retained")
            command(["docker", "load", "-i", str(package / IMAGE_ARCHIVE)], "Load product image")
            info = json.loads(command(["docker", "image", "inspect", image], "Inspect product image"))[0]
            labels = info["Config"].get("Labels") or {}
            require(info["Os"] == "linux" and info["Architecture"] == "amd64"
                    and labels.get("org.opencontainers.image.revision") == sha
                    and labels.get("org.opencontainers.image.version") == version, "Unexpected product image")
            site = f"exllm-{sha[:12]}-{uuid.uuid4().hex[:8]}"
            with tempfile.TemporaryDirectory(dir=self.releases) as staging:
                staged = Path(staging) / "site"
                stage_site(package / "exllm-site.tar.gz", self.current_site(), staged)
                os.replace(staged, self.releases / site)
        return {"commit": sha, "image": image, "image_id": info["Id"], "version": version, "site": site}

    def release(self, operation, sha):
        state = json.loads(self.state_path.read_text())
        before = state["current"]
        if state.pop("pending", None):
            print(f"Restoring interrupted release to {before['commit']}", flush=True)
            self.activate(before)
            self.write_state(state)
            if operation == "rollback":
                return
        require(self.current_site().name == before["site"] and self.running_image()[0] == before["image_id"],
                "Current release changed outside this receiver")
        target = self.prepare(sha, state) if operation == "deploy" else state["previous"]
        require(target is not None, "No previous release is available")
        self.write_state(dict(state, pending=target))
        try:
            self.activate(target)
            self.write_state({"current": target, "previous": before})
        except (Exception, KeyboardInterrupt):
            print(f"Activation failed; restoring {before['commit']}", flush=True)
            self.activate(before)
            self.write_state(state)
            print(f"Restored and verified {before['commit']}", flush=True)
            raise
        print(f"Active: {target['commit']} ({target['version']}); previous: {before['commit']}", flush=True)


def main(args):
    valid_sha = len(args) == 2 and COMMIT.fullmatch(args[1])
    require(args == ["rollback"] or (valid_sha and args[0] in ("deploy", "bootstrap")), "Invalid release command")
    with open(PRIVATE_CONFIG) as file:
        release = Release(json.load(file))
    with open(release.site_root / ".release.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if args[0] == "bootstrap":
            release.bootstrap(args[1])
        else:
            release.release(args[0], args[1] if valid_sha else None)


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except RuntimeError as err:
        sys.exit(str(err))
    except (Exception, KeyboardInterrupt) as err:
        sys.exit("Release stopped: " + type(err).__name__)
