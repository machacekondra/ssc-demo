"""Request guards, POM scope checks, and GitLab MR lifecycle for Renovate."""

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

import prepare


RUNTIME = Path(os.environ.get("RENOVATE_RUNTIME_DIR", "/work/runtime"))
STATE = RUNTIME / "request.json"
BASE = Path("/work/base")
CANDIDATE = Path("/candidate/project")
EVIDENCE = Path("/evidence")
VERIFY_MARKER = Path("/candidate/RUN_VERIFY")


def load():
    return json.loads(STATE.read_text())


def save(state):
    RUNTIME.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state))
    STATE.chmod(0o600)


def result(name, value):
    destination = os.environ.get("RESULT_" + name)
    if destination:
        Path(destination).write_text(str(value))


def api(path, method="GET", body=None):
    config = prepare.settings_config()
    context = ssl.create_default_context()
    if os.environ.get("RENOVATE_CA_BUNDLE"):
        context.load_verify_locations(os.environ["RENOVATE_CA_BUNDLE"])
    request = urllib.request.Request(
        config["gitlabEndpoint"].rstrip("/") + path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"PRIVATE-TOKEN": os.environ["RENOVATE_TOKEN"], "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, context=context, timeout=45) as response:
        return json.load(response)


def project_path(state):
    return "/projects/" + str(state["projectId"])


def preflight():
    for key in ("STATUS", "MR_URL", "MR_IID", "BRANCH", "COMMIT_SHA"):
        result(key, "")
    config = prepare.settings_config()
    repository = os.environ["TARGET_REPOSITORY"]
    if repository not in config["repositories"]:
        raise ValueError("Repository is outside the configured Renovate allowlist")
    selected = os.environ.get("SELECTED", "1")
    if selected not in ("0", "1"):
        raise ValueError("SELECTED must be 0 or 1")
    state = {"repository": repository, "status": "skipped" if selected == "0" else "active"}
    if selected == "0":
        save(state)
        result("STATUS", "skipped")
        print("Remediation skipped because SELECTED=0")
        return
    for env, key in (("TARGET_PACKAGE", "package"), ("TARGET_VERSION", "fixedVersion"),
                     ("CURRENT_VERSION", "currentVersion"), ("CVE_ID", "cveId"),
                     ("BASE_BRANCH", "baseBranch"), ("SUBDIRECTORY", "subdirectory")):
        state[key] = os.environ.get(env, "")
        if not state[key]:
            raise ValueError(env + " is required")
    if not re.fullmatch(r"CVE-\d{4}-\d{4,}", state["cveId"]):
        raise ValueError("CVE_ID must identify a CVE")
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*:[A-Za-z0-9_][A-Za-z0-9_.-]*", state["package"]):
        raise ValueError("PACKAGE must be groupId:artifactId")
    for key in ("fixedVersion", "currentVersion"):
        if len(state[key]) > 256 or re.search(r"[\s/\\]", state[key]):
            raise ValueError("Versions must be exact Maven versions")
    directory = PurePosixPath(state["subdirectory"])
    if directory.is_absolute() or ".." in directory.parts or "\\" in state["subdirectory"]:
        raise ValueError("SUBDIRECTORY must remain inside the repository")
    state["pomPath"] = str(directory / "pom.xml")
    state["justification"] = os.environ.get("JUSTIFICATION", "")[:8000]
    state["issueIid"] = os.environ.get("ISSUE_IID", "")
    requester = os.environ.get("REQUESTER_ID", "")
    note = os.environ.get("NOTE_ID", "")
    for value in (state["issueIid"], requester, note):
        if value and not value.isdecimal():
            raise ValueError("Issue, requester and note IDs must be numeric")
    if (requester or note) and not (requester and note and state["issueIid"]):
        raise ValueError("Webhook requests require issue, requester and note IDs")
    project = api("/projects/" + urllib.parse.quote(repository, safe=""))
    state["projectId"] = project["id"]
    if project.get("archived"):
        raise ValueError("Cannot remediate an archived project")
    if state["issueIid"]:
        issue = api(project_path(state) + "/issues/" + state["issueIid"])
        if issue.get("state") != "opened":
            raise ValueError("The originating issue is no longer open")
    if requester:
        try:
            member = api(project_path(state) + "/members/all/" + requester)
            authorized = member.get("access_level", 0) >= 30
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
            authorized = bool(api("/users/" + requester).get("is_admin"))
        if not authorized:
            raise ValueError("The commenter must have Developer access or higher")
        comment = api(project_path(state) + "/issues/" + state["issueIid"] + "/notes/" + note)
        if (str(comment["author"]["id"]) != requester or comment.get("system")
                or not re.match(r"^/remediate-renovate(?:\s|$)", comment.get("body", ""))):
            raise ValueError("The webhook does not match an authorized Renovate command")
    key = hashlib.sha256(json.dumps([
        repository, state["baseBranch"], state["package"], state["fixedVersion"]
    ]).encode()).hexdigest()[:20]
    state["branch"] = "renovate-remediate/" + key + "/fix"
    state["dryRun"] = os.environ.get("DRY_RUN", "false") == "true"
    if os.environ.get("DRY_RUN", "false") not in ("true", "false"):
        raise ValueError("DRY_RUN must be true or false")
    state["runName"] = os.environ.get("PIPELINE_RUN_NAME", "manual")
    save(state)
    print("Authorized Renovate request for " + repository + ": " + state["package"] + " -> " + state["fixedVersion"])


def node_at(root, indexes):
    for index in indexes:
        root = list(root)[index]
    return root


def index_of(root, sought, prefix=()):
    if root is sought:
        return list(prefix)
    for index, child in enumerate(root):
        found = index_of(child, sought, prefix + (index,))
        if found is not None:
            return found
    return None


def inspect_pom():
    state = load()
    if state["status"] != "active":
        return
    base = BASE
    root = prepare.xml_root((base / state["pomPath"]).read_bytes())
    edits = []
    found = 0
    for dependency in root.findall("dependencies/dependency") + root.findall("dependencyManagement/dependencies/dependency"):
        coordinate = (dependency.findtext("groupId") or "") + ":" + (dependency.findtext("artifactId") or "")
        if coordinate != state["package"]:
            continue
        found += 1
        if dependency.findtext("scope") == "import":
            raise ValueError("BOM imports require a separate remediation policy")
        version = dependency.find("version")
        if version is None:
            raise ValueError("The dependency is BOM-managed without an editable version")
        expression = (version.text or "").strip()
        property_match = re.fullmatch(r"\$\{([^}]+)\}", expression)
        if property_match:
            property_name = property_match[1]
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", property_name):
                raise ValueError("The version property name is unsupported")
            property_node = root.find("properties/" + property_name)
            if property_node is None:
                raise ValueError("Inherited version properties are not supported by this action")
            # Changing a shared property would update unrelated dependencies.
            references = [element for element in root.iter() if (element.text or "").strip() == expression]
            if any(reference is not version for reference in references):
                raise ValueError("The version property is shared; refusing an unrelated dependency change")
            for pom in base.rglob("pom.xml"):
                if pom != base / state["pomPath"] and expression in pom.read_text():
                    raise ValueError("The version property is referenced by another Maven module")
            version = property_node
        current = (version.text or "").strip()
        if current not in (state["currentVersion"], state["fixedVersion"]):
            raise ValueError("The issue decision is stale: the current POM version differs")
        if current != state["fixedVersion"]:
            edits.append(index_of(root, version))
    if not found:
        raise ValueError("The package is not directly declared in the project POM; transitive overrides are unsupported")
    state["edits"] = edits
    if not edits:
        state["status"] = "already-fixed"
        result("STATUS", "already-fixed")
        print("The requested fixed version is already declared; no MR is needed")
    save(state)


def find_mr(state):
    query = urllib.parse.urlencode({"state": "opened", "source_branch": state["branch"], "target_branch": state["baseBranch"]})
    matches = api(project_path(state) + "/merge_requests?" + query)
    if len(matches) > 1:
        raise ValueError("More than one MR exists for the deterministic remediation branch")
    return matches[0] if matches else None


def redraft():
    state = load()
    if state["status"] != "active" or state["dryRun"]:
        return
    mr = find_mr(state)
    if mr and not mr.get("draft", mr.get("work_in_progress", False)):
        api(project_path(state) + "/merge_requests/" + str(mr["iid"]), "PUT", {"title": "Draft: " + mr["title"]})


def collect():
    state = load()
    if state["status"] != "active":
        return
    if state["dryRun"]:
        state["status"] = "dry-run"
        save(state)
        result("STATUS", "dry-run")
        return
    mr = find_mr(state)
    if not mr:
        raise ValueError("Renovate produced no MR for the requested exact version; inspect its lookup output")
    if not mr.get("draft", mr.get("work_in_progress", False)):
        api(project_path(state) + "/merge_requests/" + str(mr["iid"]), "PUT", {"title": "Draft: " + mr["title"]})
    state.update(status="changed", mrIid=mr["iid"], mrUrl=mr["web_url"], candidateSha=mr["sha"])
    save(state)
    for key, value in (("STATUS", "draft"), ("MR_URL", state["mrUrl"]), ("MR_IID", state["mrIid"]),
                       ("BRANCH", state["branch"]), ("COMMIT_SHA", state["candidateSha"])):
        result(key, value)
    print("Draft remediation MR: " + state["mrUrl"])


def semantic(element):
    return [element.tag, sorted(element.attrib.items()), (element.text or "").strip(), [semantic(child) for child in element]]


def check_change():
    state = load()
    if state["status"] != "changed":
        return
    changed = json.loads((RUNTIME / "changed-files.json").read_text())
    if changed != [state["pomPath"]]:
        raise ValueError("The candidate must change only the requested project POM")
    expected = prepare.xml_root((BASE / state["pomPath"]).read_bytes())
    for indexes in state["edits"]:
        node_at(expected, indexes).text = state["fixedVersion"]
    actual = prepare.xml_root((CANDIDATE / state["pomPath"]).read_bytes())
    if semantic(expected) != semantic(actual):
        raise ValueError("The candidate contains changes beyond the requested dependency version")
    VERIFY_MARKER.touch()
    print("POM change scope verified; Maven verification may run")


def check_dependency():
    state = load()
    if state["status"] != "changed":
        return
    if not (EVIDENCE / "maven-verified").exists():
        raise ValueError("Maven verification did not complete")
    tree = json.loads((EVIDENCE / "dependency-tree.json").read_text())
    versions = set()

    def visit(node):
        if node.get("groupId", "") + ":" + node.get("artifactId", "") == state["package"]:
            versions.add(node.get("version"))
        for child in node.get("children", []):
            visit(child)

    visit(tree)
    if versions != {state["fixedVersion"]}:
        raise ValueError("The resolved dependency tree does not contain only the requested fixed version")
    state["verified"] = True
    save(state)
    print("Maven tests and resolved dependency version verified")


def finalize():
    state = load()
    if state["status"] != "changed":
        return
    if not state.get("verified"):
        raise ValueError("Candidate has not passed verification")
    mr = api(project_path(state) + "/merge_requests/" + str(state["mrIid"]))
    base = api(project_path(state) + "/repository/branches/" + urllib.parse.quote(state["baseBranch"], safe=""))
    if mr["state"] != "opened" or mr["sha"] != state["candidateSha"] or base["commit"]["id"] != state["baseSha"]:
        raise ValueError("The MR or target branch moved during verification; rerun the action")
    title = re.sub(r"^(?:Draft:|WIP:|\[Draft\]|\(Draft\))\s*", "", mr["title"], flags=re.I)
    description = mr.get("description") or ""
    description = re.sub(r"\n<!-- tekton-renovate-verification -->.*?<!-- /tekton-renovate-verification -->", "", description, flags=re.S)
    description += ("\n<!-- tekton-renovate-verification -->\n"
                    f"Verified by Tekton `{state['runName']}`: `mvn verify` passed and "
                    f"`{state['package']}` resolves to `{state['fixedVersion']}`.\n"
                    "<!-- /tekton-renovate-verification -->")
    api(project_path(state) + "/merge_requests/" + str(state["mrIid"]), "PUT", {"title": title, "description": description})
    result("STATUS", "ready")
    for key, value in (("MR_URL", state["mrUrl"]), ("MR_IID", state["mrIid"]),
                       ("BRANCH", state["branch"]), ("COMMIT_SHA", state["candidateSha"])):
        result(key, value)
    print("Verified remediation MR is ready for human review: " + state["mrUrl"])


def main():
    command = sys.argv[1]
    if command == "prepare":
        if load()["status"] == "active":
            prepare.prepare()
        return
    {"preflight": preflight, "inspect": inspect_pom, "redraft": redraft,
     "collect": collect, "check-change": check_change, "check-dependency": check_dependency,
     "finalize": finalize}[command]()


if __name__ == "__main__":
    try:
        main()
    except urllib.error.HTTPError as error:
        sys.exit(f"Renovate remediation failed: HTTP {error.code}; check GitLab/Artifactory access")
    except (ValueError, KeyError, OSError, ET.ParseError, urllib.error.URLError) as error:
        sys.exit("Renovate remediation failed: " + str(error))
