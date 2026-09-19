Based on what I found, there is a plausible analog, but I was unable to locate the underlying Java implementation (`SingleExtensionEvalFunction`, `LockFileModuleExtension`, `BazelLockFileFunction`) in this index — only documentation, the `MODULE.bazel.lock` merge tooling, and Python integration tests are indexed for this feature. I'll flag that gap explicitly at the end.

### Title
Module-extension `facts` in `MODULE.bazel.lock` are trusted and merged from externally-supplied (attacker-controlled) lockfile content without re-verification for non-reproducible extensions - ([File: src/test/py/bazel/bzlmod/bazel_lockfile_test.py])

### Summary
Bazel's Bzlmod `facts` mechanism lets a module extension persist "immutable" data (the docs give the canonical example of a URL/checksum pair for an SDK version) in `MODULE.bazel.lock` so future evaluations skip the network fetch [1](#0-0) . For non-reproducible extensions, Bazel's default `--lockfile_mode=update` explicitly keeps facts that were injected into the workspace lockfile by "a branch switch or pull" without re-running the extension to validate them, reasoning that "a cold machine would not rerun the extension either" [2](#0-1) .

### Finding Description
Two independent sources determine the same extension's `facts` data, analogous to TrueFi's on-chain vs. off-chain loan channels that are never reconciled:
1. The extension's live implementation function, which is supposed to fetch/verify data (e.g., checksums) from the network.
2. The `facts` entry recorded in the checked-in `MODULE.bazel.lock` file, which travels with the source tree (branches, PRs, forks).

The `testExternallyUpdatedFactsKeptByUpdateModeForNonReproducibleExtension` test demonstrates that if the workspace lockfile's `facts` for a non-reproducible extension are edited externally (simulating "a branch switch or pull that brings in a re-pin performed on another machine"), Bazel keeps the new facts as-is in `--lockfile_mode=update` (the default) without ever re-invoking the extension to check them against a real fetch [3](#0-2) . This is intentional design, but it means whoever can edit `MODULE.bazel.lock` on a branch a victim checks out (an "outsider ... files on an untrusted branch CI builds") controls the `facts` values consumed by the extension, since `module_ctx.facts` merely returns whatever is stored under the extension's key, "may have been created by a different version of the extension" and is not otherwise authenticated [4](#0-3) .

The documentation explicitly recommends this pattern for data "you don't have a checksum available to guard the download" — i.e., precisely the case where there is no independent integrity check to catch a forged fact [1](#0-0) . The lockfile-merge tooling (`scripts/bazel-lockfile-merge.jq`) further shows that `facts` from multiple lockfiles are shallow-merged with "last wins" semantics per key [5](#0-4) [6](#0-5) , meaning a single malicious entry contributed by an attacker-controlled branch/lockfile can silently override or add to legitimately-produced facts for a shared extension, and this forged value then propagates into every subsequent build that merges or inherits that lockfile — the two "loan" channels (real network-verified facts vs. externally-injected lockfile facts) are combined without reconciliation, exactly like TrueFi's isolated on-chain/off-chain loan approval paths.

### Impact Explanation
If an extension (as Bazel's own docs suggest) stores "download URL and checksum" pairs in `facts` to avoid re-fetching from the network, an attacker who can get a victim to build from a branch/PR/fork with a tampered `MODULE.bazel.lock` can inject a forged fact — e.g., mapping a legitimate-looking key to an attacker-chosen URL/checksum pair. Because `--lockfile_mode=update` (the default) accepts this without re-running the extension against the network, the extension will treat the attacker's value as trusted "immutable" data and use it (for example, to configure a `ctx.download()` with an attacker-supplied checksum), which can result in fetching and trusting attacker-controlled content in place of the real dependency — a containment/integrity bypass at the repository-rule layer that reaches every subsequent build sharing that lockfile state.

### Likelihood Explanation
Likely wherever an organization uses non-reproducible module extensions with `facts` (the documented, encouraged pattern), and where CI builds pull requests/branches from external or semi-trusted contributors. No credentials, MITM, or origin-server compromise is needed — only the ability to modify the `MODULE.bazel.lock` file on a branch that gets built, which matches the permitted "unprivileged outsider ... untrusted branch CI builds" attacker model.

### Recommendation
Bind `facts` entries to a verifiable digest supplied independently of the lockfile (e.g., require the extension's tag inputs or a registry-pinned hash to co-validate any externally-supplied fact before it's trusted), or require `--lockfile_mode=error`/re-evaluation whenever a non-reproducible extension's facts changed on a lockfile the current invocation did not itself write, rather than silently accepting externally introduced entries as ground truth.

### Proof of Concept
The behavior is already demonstrated by Bazel's own test suite, which can serve as the reproduction: `testExternallyUpdatedFactsKeptByUpdateModeForNonReproducibleExtension` sets up a non-reproducible extension with facts, manually injects an extra facts entry into `MODULE.bazel.lock` (simulating a branch pull), and confirms Bazel's build under `--lockfile_mode=update` keeps the injected value without re-evaluating the extension [7](#0-6) . Replacing the injected value with an attacker-chosen URL/checksum pair for a `ctx.download()`-driven extension would demonstrate the integrity-bypass impact concretely.

---
**Coverage caveat:** I could not find the Java implementation (`SingleExtensionEvalFunction`, `LockFileModuleExtension`, `BazelLockFileFunction`) that performs this facts merge/validation in this index — only documentation, the `jq`-based lockfile-merge tooling, and Python integration tests were available. Due to index size limits, some file contents (particularly `src/main/java/.../bazel/bzlmod/*.java`) were not retrievable. If you need the exact method/line where facts validation happens, I'd recommend starting a Devin session with full repo access to pinpoint it.

### Citations

**File:** docs/versions/9.1.0/external/extension.mdx (L257-267)
```text
If your extension relies on effectively immutable data obtained from outside
the build, most commonly from the network, but you don't have a checksum
available to guard the download, consider using the `facts` parameter of
[`extension_metadata`](/versions/9.1.0/rules/lib/builtins/module_ctx#extension_metadata) to
persistently record such data and thus allow your extension to become
reproducible. `facts` is expected to be a dictionary with string keys and
arbitrary JSON-like Starlark values that is always persisted in the lockfile and
available to future evaluations of the extension via the
[`facts`](/versions/9.1.0/rules/lib/builtins/module_ctx#facts) field of `module_ctx`.

`facts` are not invalidated even when the code of your module extension changes,
```

**File:** src/test/py/bazel/bzlmod/bazel_lockfile_test.py (L3725-3756)
```python
  def testExternallyUpdatedFactsKeptByUpdateModeForNonReproducibleExtension(
      self,
  ):
    """UPDATE mode keeps updated facts of a non-reproducible extension.

    The hidden lockfile records the facts produced by the most recent local
    evaluation of the extension. If the workspace lockfile's facts for a
    non-reproducible extension change without any of its inputs changing (e.g.
    a branch switch or pull brings in a re-pin performed on another machine),
    the cached result must be reused with the new facts as is: a cold machine
    would not rerun the extension either and a rerun could require network
    access or credentials that are only available to the person updating the
    pins.
    """
    extension_id = self._setUpExtensionWithFacts(reproducible=False)
    self._addFactToLockfile(extension_id, '1.27.0', {'hash': 'hola'})

    _, _, stderr = self.RunBazel(
        ['build', '@hello//:all', '--lockfile_mode=update']
    )
    self.assertNotIn('lockfile_ext is being evaluated', '\n'.join(stderr))

    with open(self.Path('MODULE.bazel.lock'), 'r') as f:
      lockfile = json.loads(f.read().strip())
    self.assertEqual(
        lockfile['facts'][extension_id],
        {
            '1.25.0': {'hash': 'olleh'},
            '1.26.1': {'hash': 'hello'},
            '1.27.0': {'hash': 'hola'},
        },
    )
```

**File:** docs/versions/9.1.0/rules/lib/builtins/module_ctx.mdx (L128-141)
```text
## facts

```
Facts module_ctx.facts
```

The JSON-like dict returned by a previous execution of this extension in the `facts`
parameter of [`extension_metadata`](/versions/9.1.0/rules/lib/builtins/module_ctx#extension_metadata) or else
`&lcub;&rcub;`.
This is useful for extensions that want to preserve universally true facts such as the
hashes of artifacts in an immutable repository.
Note that the returned value may have been created by a different version of the
extension, which may have used a different schema.

```

**File:** scripts/bazel-lockfile-merge.jq (L17-30)
```text
# Given an array of objects, shallowly merges the result of applying f to each
# object into a single object, with a few special properties:
# 1. Values are uniquified before merging and then merged with last-wins
#    semantics. Assuming that the first value is the base, this ensures that
#    later occurrences of the base value do not override other values. For
#    example, when this is called with B A1 A2 and A1 contains changes to a
#    field but A2 does not (compared to B), the changes in A1 will be preserved.
# 2. Object keys on the top level are sorted lexicographically after merging,
#    but are additionally split on ":". This ensures that module extension IDs,
#    which start with labels, sort as strings in the same way as they due as
#    structured objects in Bazel (that is, //python/extensions:python.bzl
#    sorts before //python/extensions/private:internal_deps.bzl).
def shallow_merge(f):
  map(f) | stable_unique | add | to_entries | sort_by(.key | split(":")) | from_entries;
```

**File:** scripts/bazel_lockfile_merge_test.sh (L273-381)
```shellscript
function test_merge_facts() {
  cat > base <<'EOF'
{
  "lockFileVersion": 28,
  "registryFileHashes": {
    "https://example.org/modules/foo/1.0/MODULE.bazel": "1234"
  },
  "selectedYankedVersions": {},
  "moduleExtensions": {}
}
EOF
  cat > left <<'EOF'
{
  "lockFileVersion": 28,
  "registryFileHashes": {
    "https://example.org/modules/foo/1.0/MODULE.bazel": "1234"
  },
  "selectedYankedVersions": {},
  "moduleExtensions": {},
  "facts": {
    "@@rules_foo+//:foo_deps.bzl%foo_deps": {
      "json_foo@1.0": {
        "sha256": "1111",
        "url": "https://example.org/json_foo/1.0.zip"
      },
      "cbor_foo@2.0": {
        "sha256": "2222",
        "url": "https://example.org/cbor_foo/2.0.zip"
      }
    },
    "@@rules_bar+//:bar_deps.bzl%bar_deps": {
      "json_bar@1.0": {
        "sha256": "3333",
        "url": "https://example.org/json_bar/1.0.zip"
      }
    }
  }
}
EOF
  cat > right <<'EOF'
{
  "lockFileVersion": 28,
  "registryFileHashes": {
    "https://example.org/modules/foo/1.0/MODULE.bazel": "1234"
  },
  "selectedYankedVersions": {},
  "moduleExtensions": {},
  "facts": {
    "@@rules_foo+//:foo_deps.bzl%foo_deps": {
      "json_foo@1.0": {
        "sha256": "1111",
        "url": "https://example.org/json_foo/1.0.zip"
      },
      "xml_foo@3.0": {
        "sha256": "4444",
        "url": "https://example.org/xml_foo/3.0.zip"
      }
    },
    "@@rules_baz+//:baz_deps.bzl%baz_deps": {
      "json_baz@1.0": {
        "sha256": "5555",
        "url": "https://example.org/json_baz/1.0.zip"
      }
    }
  }
}
EOF
  cat > expected <<'EOF'
{
  "lockFileVersion": 28,
  "registryFileHashes": {
    "https://example.org/modules/foo/1.0/MODULE.bazel": "1234"
  },
  "selectedYankedVersions": {},
  "moduleExtensions": {},
  "facts": {
    "@@rules_bar+//:bar_deps.bzl%bar_deps": {
      "json_bar@1.0": {
        "sha256": "3333",
        "url": "https://example.org/json_bar/1.0.zip"
      }
    },
    "@@rules_baz+//:baz_deps.bzl%baz_deps": {
      "json_baz@1.0": {
        "sha256": "5555",
        "url": "https://example.org/json_baz/1.0.zip"
      }
    },
    "@@rules_foo+//:foo_deps.bzl%foo_deps": {
      "cbor_foo@2.0": {
        "sha256": "2222",
        "url": "https://example.org/cbor_foo/2.0.zip"
      },
      "json_foo@1.0": {
        "sha256": "1111",
        "url": "https://example.org/json_foo/1.0.zip"
      },
      "xml_foo@3.0": {
        "sha256": "4444",
        "url": "https://example.org/xml_foo/3.0.zip"
      }
    }
  }
}
EOF

  do_merge base left right
  diff -u expected left || fail "output differs"
}
```
