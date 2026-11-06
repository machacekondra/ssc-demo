"""Prepare Renovate auth from the same Maven settings used by the build."""

import base64
import json
import os
from pathlib import Path
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET


def settings_config():
    return json.loads(Path(os.environ.get(
        "RENOVATE_SETTINGS_FILE", "/etc/renovate/settings.json"
    )).read_text())


def xml_root(data):
    root = ET.fromstring(data)
    for element in root.iter():
        element.tag = element.tag.rsplit("}", 1)[-1]
    return root


def prepare():
    config = settings_config()
    repository = os.environ["TARGET_REPOSITORY"]
    package = os.environ["TARGET_PACKAGE"]
    target = os.environ.get("TARGET_VERSION", "")
    if repository not in config["repositories"]:
        raise ValueError("Repository is outside the configured Renovate allowlist")
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*:[A-Za-z0-9_][A-Za-z0-9_.-]*", package):
        raise ValueError("PACKAGE must be a Maven groupId:artifactId")
    if target and (len(target) > 256 or re.search(r"[\s/\\]", target)):
        raise ValueError("FIXED_VERSION must be an exact Maven version")

    root = xml_root(Path(os.environ["MAVEN_SETTINGS_FILE"]).read_bytes())
    mirror = next((m for m in root.findall("mirrors/mirror")
                   if m.findtext("id") == config["mirrorId"]), None)
    if mirror is None:
        raise ValueError("Configured Artifactory mirror ID is missing from settings.xml")
    registry = config["registryUrl"].rstrip("/")
    parsed = urllib.parse.urlsplit(registry)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Artifactory registryUrl must be an HTTP(S) URL without credentials")
    if (mirror.findtext("url") or "").rstrip("/") != registry:
        raise ValueError("Artifactory registryUrl must match the selected Maven mirror URL")
    server = next((s for s in root.findall("servers/server")
                   if s.findtext("id") == config["mirrorId"]), None)
    if server is None:
        raise ValueError("Maven mirror has no matching server credential")
    username = server.findtext("username") or ""
    password = server.findtext("password") or ""
    if not username or not password or "${" in username or "${" in password:
        raise ValueError("Maven server credentials must be populated, not environment placeholders")
    if password.startswith("{") and password.endswith("}"):
        raise ValueError("Encrypted Maven passwords are unsupported; use a service-account credential")

    runtime = Path(os.environ.get("RENOVATE_RUNTIME_DIR", "/work/runtime"))
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Only ephemeral pod storage contains the generated credentials. Never log it.
    auth_file = runtime / "artifactory.json"
    fd = os.open(auth_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump({"username": username, "password": password}, stream)

    context = ssl.create_default_context()
    if os.environ.get("RENOVATE_CA_BUNDLE"):
        context.load_verify_locations(os.environ["RENOVATE_CA_BUNDLE"])
    auth = "Basic " + base64.b64encode((username + ":" + password).encode()).decode()
    group, artifact = package.split(":")
    package_url = registry + "/" + group.replace(".", "/") + "/" + artifact

    def fetch(url):
        request = urllib.request.Request(url, headers={"Authorization": auth})
        with urllib.request.urlopen(request, context=context, timeout=45) as response:
            return response.read()

    metadata = xml_root(fetch(package_url + "/maven-metadata.xml"))
    versions = [node.text for node in metadata.findall("versioning/versions/version") if node.text]
    if not versions:
        raise ValueError("Artifactory metadata contains no package versions")
    if target:
        if target not in versions:
            raise ValueError("Requested FIXED_VERSION is absent from Artifactory metadata")
        encoded = urllib.parse.quote(target, safe="")
        xml_root(fetch(f"{package_url}/{encoded}/{artifact}-{encoded}.pom"))
    summary = {
        "repository": repository, "package": package, "registryUrl": registry,
        "mirrorId": config["mirrorId"], "versionCount": len(versions),
        "lightwellVersions": [v for v in versions if re.search(r"[.-]rhlw-", v)][-10:],
        "targetVersion": target or None,
    }
    (runtime / "registry-check.json").write_text(json.dumps(summary))
    print("Artifactory lookup succeeded: " + json.dumps(summary), flush=True)


if __name__ == "__main__":
    try:
        prepare()
    except urllib.error.HTTPError as error:
        sys.exit(f"Artifactory lookup failed: HTTP {error.code}; check repository access and metadata")
    except (ValueError, KeyError, OSError, ET.ParseError, urllib.error.URLError) as error:
        sys.exit("Renovate preparation failed: " + str(error))
