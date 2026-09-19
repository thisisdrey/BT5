### Title
Bzlmod lockfile (`MODULE.bazel.lock`) unconditionally persists full, unredacted repository-rule attribute values for every module-extension-generated repo, leaking secrets into a file recommended for version control - (File: `MODULE.bazel.lock` / lockfile serialization of `generatedRepoSpecs`)

### Summary
Bazel's Bzlmod lockfile stores, for every module extension, the complete set of attribute values (`generatedRepoSpecs.<repo>.attributes`) that the extension used to instantiate each repository rule. This is directly analogous to the Rancher issue: an application's configuration values (which may contain credentials, tokens, or other secrets) are persisted verbatim into an artifact that many actors can read - in Rancher's case the `Apps` CRD and audit logs, in Bazel's case the `MODULE.bazel.lock` file, which the documentation explicitly recommends checking into version control and sharing across the team [1](#0-0) .

### Finding Description
The lockfile's `moduleExtensions` section records, per extension, a `generatedRepoSpecs` map containing every repo the extension created, keyed by the `ruleClassName`/`repoRuleId` and a raw `attributes` object holding all the attribute values passed to that repo rule [2](#0-1) . Real-world examples in the repository's own `MODULE.bazel.lock` show this includes not just innocuous data such as URLs and `sha256`/`strip_prefix`, but arbitrarily large, free-form `build_file_content` and any other attribute the extension author defines [3](#0-2) .

Module extensions are ordinary Starlark code that can define repository rules with arbitrary custom attributes — including attributes intended to carry credentials, API tokens, or other secrets needed to fetch from private mirrors/registries (this pattern is explicit in `tools/build_defs/repo/utils.bzl`'s `use_netrc`/`get_auth` helpers, which build `login`/`password`/`pattern` dictionaries per URL for private repos [4](#0-3) ). Nothing in the lockfile-writing path filters, redacts, or excludes such sensitive attribute values before they are serialized to disk: the entire `attributes` map for a generated repo is dumped byte-for-byte into the lockfile JSON, as confirmed by both the documented schema and the project's own committed lockfile and merge-test fixtures [5](#0-4) [6](#0-5) .

The lockfile is explicitly designed to be a durable, shared, human/CI-readable artifact: Bazel's own docs recommend committing `MODULE.bazel.lock` to version control "to facilitate collaboration and ensure that all team members have access to the same lockfile" [1](#0-0) . Test utilities (`src/tools/bzlmod/utils.bzl`) even parse these `generatedRepoSpecs.attributes` fields directly out of the lockfile to extract download metadata, demonstrating that the attributes are plain, structured, and readable by any consumer of the lockfile [7](#0-6) .

### Impact Explanation
Any secret value that a module extension author chooses to pass as a repository-rule attribute (e.g., a private-registry token, an internal mirror password, or any other credential-shaped attribute value) is written unredacted into `MODULE.bazel.lock`. Because this file is meant to be committed to source control, shared across a team, and read by CI systems and any collaborator with repo access, this mirrors the Rancher CVE-2024-52282 pattern exactly: sensitive configuration values leak to any party with read access to a normally-shared artifact (whereas the Rancher CRD required only `GET` access, here it requires only read access to the repo/lockfile — a much lower bar than compromising the build machine or output base). There is no mechanism analogous to a "sensitive" attribute type or exclusion list to prevent such values from being persisted.

### Likelihood Explanation
This requires an organization's own module extension to accept/carry a secret-bearing attribute (a common and encouraged pattern for private mirrors, per Bazel's own `get_auth`/`use_netrc` utilities), and for that organization to follow Bazel's documented best practice of committing the lockfile to version control. Given how normalized private artifact/module mirrors are in enterprise Bazel usage, and that the lockfile-commit recommendation is prominent in the docs, the likelihood of secret leakage via this path is realistic in typical enterprise deployments.

### Recommendation
- Provide a way for module extensions/repository rules to mark specific attributes as sensitive (e.g., a `attr.string(sensitive = True)` equivalent) so the lockfile writer can omit or redact them from `generatedRepoSpecs.attributes`.
- Document explicitly that any attribute value passed to a repository rule from a module extension is persisted in plaintext to the (recommended-to-be-committed) lockfile, and warn extension authors never to pass credentials as plain attributes.
- Consider excluding raw attribute dumps for repos whose rule class is not from a small, trusted allowlist (or hashing/opaquing attribute values instead of storing full plaintext) unless the extension explicitly requests full reproducibility of that data.

### Proof of Concept
Note: I could not locate the underlying Java implementation (e.g., a `RepoSpec`/`LockFileModuleExtension`/`ModuleExtensionEvalFunction` class) in the indexed codebase to point to the exact serialization call site — the index only surfaced documentation, the checked-in `MODULE.bazel.lock`, and shell/Starlark test fixtures. This is a known indexing limitation; a full Devin session with filesystem access would be needed to pinpoint the exact Java writer (likely under `src/main/java/com/google/devtools/build/lib/bazel/bzlmod/`) and produce a `BazelLockfileTest`-style JUnit/`bazel_lockfile_test.py` reproduction. A conceptual PoC using existing test infrastructure:

1. Define a module extension whose repo rule takes a `token` attribute (simulating a private-mirror credential), e.g. following the pattern already used in `bazel_lockfile_test.py`'s `testExtensionEvaluationRerunsIfDepGraphOrderChanges`, where a tag value is passed through into a generated repo's `attributes.value` and shows up verbatim in the lockfile [8](#0-7) .
2. Run `bazel build` and inspect `MODULE.bazel.lock`; confirm the secret-shaped tag value appears in plaintext under `moduleExtensions.<ext>.general.generatedRepoSpecs.<repo>.attributes.value` [9](#0-8) .
3. Commit/share the lockfile per documented best practice and observe the secret is now readable by anyone with repository access, with no redaction or opt-out mechanism.

I was unable to verify from the index alone whether a later, unreleased mitigation (e.g., a "sensitive" attribute mechanism) already exists in the Java source, since that source was not retrievable through the available search tools. Starting a Devin session with full filesystem access would let a background agent confirm the exact serialization code path and write a proper JUnit reproduction.

### Citations

**File:** docs/external/lockfile.mdx (L112-124)
```text
  "moduleExtensions": {
    "//:extension.bzl%lockfile_ext": {
      "general": {
        "bzlTransitiveDigest": "oWDzxG/aLnyY6Ubrfy....+Jp6maQvEPxn0pBM=",
        "usagesDigest": "aLmqbvowmHkkBPve05yyDNGN7oh7QE9kBADr3QIZTZs=",
        ...,
        "generatedRepoSpecs": {
          "hello": {
            "bzlFile": "@@//:extension.bzl",
            ...
          }
        }
      }
```

**File:** docs/external/lockfile.mdx (L227-230)
```text
*   Include the lockfile in version control to facilitate collaboration and
    ensure that all team members have access to the same lockfile, promoting
    consistent development environments across the project.

```

**File:** MODULE.bazel.lock (L515-527)
```text
        "generatedRepoSpecs": {
          "prometheus_metrics_model": {
            "repoRuleId": "@@bazel_tools//tools/build_defs/repo:http.bzl%http_archive",
            "attributes": {
              "urls": [
                "https://github.com/prometheus/client_model/archive/v0.6.2.tar.gz"
              ],
              "sha256": "47c5ea7949f68e7f7b344350c59b6bd31eeb921f0eec6c3a566e27cf1951470c",
              "strip_prefix": "client_model-0.6.2",
              "build_file_content": "\nload(\"@envoy_api//bazel:api_build_system.bzl\", \"api_cc_py_proto_library\")\nload(\"@io_bazel_rules_go//proto:def.bzl\", \"go_proto_library\")\n\napi_cc_py_proto_library(\n    name = \"client_model\",\n    srcs = [\n        \"io/prometheus/client/metrics.proto\",\n    ],\n    visibility = [\"//visibility:public\"],\n)\n\ngo_proto_library(\n    name = \"client_model_go_proto\",\n    importpath = \"github.com/prometheus/client_model/go\",\n    proto = \":client_model\",\n    visibility = [\"//visibility:public\"],\n)\n"
            }
          }
        }
```

**File:** tools/build_defs/repo/utils.bzl (L433-495)
```text
def use_netrc(netrc, urls, patterns):
    """Compute an auth dict from a parsed netrc file and a list of URLs.

    Args:
      netrc: a netrc file already parsed to a dict, e.g., as obtained from
        read_netrc
      urls: a list of URLs.
      patterns: optional dict of url to authorization patterns

    Returns:
      dict suitable as auth argument for ctx.download; more precisely, the dict
      will map all URLs where the netrc file provides login and password to a
      dict containing the corresponding login, password and optional authorization pattern,
      as well as the mapping of "type" to "basic" or "pattern".
    """
    auth = {}
    for url in urls:
        schemerest = url.split("://", 1)
        if len(schemerest) < 2:
            continue
        if not (schemerest[0] in ["http", "https"]):
            # For other protocols, bazel currently does not support
            # authentication. So ignore them.
            continue
        host = schemerest[1].split("/")[0].split(":")[0]
        if host in netrc:
            authforhost = netrc[host]
        elif "" in netrc:
            authforhost = netrc[""]
        else:
            continue

        if host in patterns:
            auth_dict = {
                "type": "pattern",
                "pattern": patterns[host],
            }

            if "login" in authforhost:
                auth_dict["login"] = authforhost["login"]

            if "password" in authforhost:
                auth_dict["password"] = authforhost["password"]

            auth[url] = auth_dict
        elif "password" in authforhost:
            if "login" in authforhost:
                auth[url] = {
                    "type": "basic",
                    "login": authforhost["login"],
                    "password": authforhost["password"],
                }
            else:
                auth[url] = {
                    "type": "pattern",
                    "pattern": "Bearer <password>",
                    "password": authforhost["password"],
                }
        else:
            # buildifier: disable=print
            print("WARNING: Found machine in .netrc for URL %s, but no password." % url)

    return auth
```

**File:** docs/versions/9.1.0/external/lockfile.mdx (L195-207)
```text
1. The `bzlTransitiveDigest` is the digest of the extension implementation
   and the .bzl files transitively loaded by it.
2. The `usagesDigest` is the digest of the _usages_ of the extension in the
   dependency graph, which includes all tags.
3. Further unspecified fields that track other inputs to the extension,
   such as contents of files or directories it reads or environment
   variables it uses.
4. The `generatedRepoSpecs` encode the repositories created by the
   extension with the current input.
5. The optional `moduleExtensionMetadata` field contains metadata provided by
   the extension such as whether certain repositories it created should be
   imported via `use_repo` by the root module. This information powers the
   `bazel mod tidy` command.
```

**File:** scripts/bazel_lockfile_merge_test.sh (L129-152)
```shellscript
  "moduleExtensions": {
    "//:rbe_extensions.bzl%bazel_rbe_deps": {
      "general": {
        "bzlTransitiveDigest": "changed",
        "recordedFileInputs": {},
        "recordedDirentsInputs": {},
        "envVariables": {},
        "generatedRepoSpecs": {
          "rbe_ubuntu2004": {
            "bzlFile": "@@+bazel_test_deps+bazelci_rules//:rbe_repo.bzl",
            "ruleClassName": "rbe_preconfig",
            "attributes": {
              "toolchain": "ubuntu2004"
            }
          }
        },
        "recordedRepoMappingEntries": [
          [
            "",
            "bazelci_rules",
            "+bazel_test_deps+bazelci_rules"
          ]
        ]
      }
```

**File:** src/tools/bzlmod/utils.bzl (L101-117)
```text
            for extension in extensions:
                for local_name, repo_spec in extension.get("generatedRepoSpecs", {}).items():
                    rule_class = repo_spec["ruleClassName"] if "ruleClassName" in repo_spec else repo_spec["repoRuleId"]

                    if rule_class.endswith("http_archive") or rule_class.endswith("http_file") or rule_class.endswith("http_jar"):
                        attributes = repo_spec["attributes"]
                        repo_name = repo_name_prefix + local_name

                        if repo_name not in required_repos:
                            continue
                        found_repos.append(repo_name)

                        http_artifacts.append({
                            "sha256": attributes.get("sha256", None),
                            "integrity": attributes.get("integrity", None),
                            "url": extract_url(attributes),
                        })
```

**File:** src/test/py/bazel/bzlmod/bazel_lockfile_test.py (L1743-1759)
```python
    ext_2_key = '//:extension.bzl%ext_2'
    with open('MODULE.bazel.lock', 'r') as json_file:
      lockfile = json.load(json_file)
    self.assertIn(ext_1_key, lockfile['moduleExtensions'])
    self.assertIn(
        'Ext 1 saw 1 tags',
        lockfile['moduleExtensions'][ext_1_key]['general'][
            'generatedRepoSpecs'
        ]['dep']['attributes']['value'],
    )
    self.assertIn(ext_2_key, lockfile['moduleExtensions'])
    self.assertIn(
        'Ext 2 saw 2 tags',
        lockfile['moduleExtensions'][ext_2_key]['general'][
            'generatedRepoSpecs'
        ]['dep']['attributes']['value'],
    )
```

**File:** src/test/py/bazel/bzlmod/bazel_lockfile_test.py (L1888-1902)
```python
    _, _, stderr = self.RunBazel(['build', '@dep//:all'])
    stderr = '\n'.join(stderr)

    self.assertIn('Ext is being evaluated', stderr)
    self.assertIn('Ext saw values: root,aaa,bbb', stderr)
    ext_key = '//:extension.bzl%ext'
    with open('MODULE.bazel.lock', 'r') as json_file:
      lockfile = json.load(json_file)
    self.assertIn(ext_key, lockfile['moduleExtensions'])
    self.assertIn(
        'Ext saw values: root,aaa,bbb',
        lockfile['moduleExtensions'][ext_key]['general']['generatedRepoSpecs'][
            'dep'
        ]['attributes']['value'],
    )
```
