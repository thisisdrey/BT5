import json
import os

from decouple import config

# todo: if scope_files is: 500 > 50, 300 > 30 , 100 > 10
MAX_REPO = 12
# todo: the GitLab namespace/project path, for example group/project
SOURCE_REPO = 'bazelbuild/bazel'
# todo: the name of the repository
REPO_NAME = 'bazel'

run_number = os.environ.get('GITHUB_RUN_NUMBER', '0')


def get_cyclic_index(run_number, max_index=100):
    """Convert run number to a cyclic index between 1 and max_index"""
    return (int(run_number) - 1) % max_index + 1


def load_repository_urls():
    """Load repository URLs from repositories.json."""
    repo_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "repositories.json")
    if not os.path.exists(repo_file):
        return []

    try:
        with open(repo_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []

    if not isinstance(data, list):
        return []

    return [url for url in data if isinstance(url, str) and url.strip()]


if run_number == "0":
    BASE_URL = f"https://deepwiki.com/{SOURCE_REPO}"
else:
    repository_urls = load_repository_urls()
    if repository_urls:
        run_index = get_cyclic_index(run_number, len(repository_urls))
        BASE_URL = repository_urls[run_index - 1]
    else:
        BASE_URL = f"https://deepwiki.com/{SOURCE_REPO}"

scope_files = [
    # =================================================================================
    # External artifact download: integrity checking, URL handling and retry/stream trust
    # =================================================================================
    "src/main/java/com/google/devtools/build/lib/bazel/repository/downloader/DownloadManager.java",
    "src/main/java/com/google/devtools/build/lib/bazel/repository/downloader/HttpDownloader.java",
    "src/main/java/com/google/devtools/build/lib/bazel/repository/downloader/HttpConnector.java",
    "src/main/java/com/google/devtools/build/lib/bazel/repository/downloader/HttpConnectorMultiplexer.java",
    "src/main/java/com/google/devtools/build/lib/bazel/repository/downloader/HttpStream.java",
    "src/main/java/com/google/devtools/build/lib/bazel/repository/downloader/HttpUtils.java",
    "src/main/java/com/google/devtools/build/lib/bazel/repository/downloader/Checksum.java",
    "src/main/java/com/google/devtools/build/lib/bazel/repository/downloader/HashInputStream.java",
    "src/main/java/com/google/devtools/build/lib/bazel/repository/downloader/HashOutputStream.java",
    "src/main/java/com/google/devtools/build/lib/bazel/repository/downloader/RetryingInputStream.java",
    "src/main/java/com/google/devtools/build/lib/bazel/repository/downloader/CheckContentLengthInputStream.java",
    "src/main/java/com/google/devtools/build/lib/bazel/repository/downloader/UrlRewriter.java",
    "src/main/java/com/google/devtools/build/lib/bazel/repository/downloader/UrlRewriterConfig.java",
    "src/main/java/com/google/devtools/build/lib/bazel/repository/downloader/DelegatingDownloader.java",
    "src/main/java/com/google/devtools/build/lib/bazel/repository/downloader/ProxyHelper.java",
    "src/main/java/com/google/devtools/build/lib/bazel/repository/cache/DownloadCache.java",

    # =================================================================================
    # Download cache, repo contents cache and their keying
    # =================================================================================
    "src/main/java/com/google/devtools/build/lib/bazel/repository/cache/RepositoryCache.java",
    "src/main/java/com/google/devtools/build/lib/bazel/repository/cache/LocalRepoContentsCache.java",
    "src/main/java/com/google/devtools/build/lib/remote/RemoteRepoContentsCacheImpl.java",
    "src/main/java/com/google/devtools/build/lib/bazel/repository/decompressor/DecompressorValue.java",

    # =================================================================================
    # Archive extraction and patching: entry paths, symlinks and prefix stripping
    # =================================================================================
    "src/main/java/com/google/devtools/build/lib/bazel/repository/decompressor/ZipDecompressor.java",
    "src/main/java/com/google/devtools/build/lib/bazel/repository/decompressor/TarFunction.java",
    "src/main/java/com/google/devtools/build/lib/bazel/repository/decompressor/CompressedTarFunction.java",
    "src/main/java/com/google/devtools/build/lib/bazel/repository/decompressor/StripPrefixedPath.java",
    "src/main/java/com/google/devtools/build/lib/bazel/repository/decompressor/ArFunction.java",
    "src/main/java/com/google/devtools/build/lib/bazel/repository/decompressor/SevenZDecompressor.java",
    "src/main/java/com/google/devtools/build/lib/bazel/repository/decompressor/CompressedFunction.java",
    "src/main/java/com/google/devtools/build/lib/bazel/repository/decompressor/PatchUtil.java",
    "src/main/java/com/google/devtools/build/lib/bazel/repository/starlark/StarlarkBaseExternalContext.java",

    # =================================================================================
    # Repository rule execution context: download, extract, symlink, file and path handling
    # =================================================================================
    "src/main/java/com/google/devtools/build/lib/bazel/repository/starlark/StarlarkRepositoryContext.java",
    "src/main/java/com/google/devtools/build/lib/bazel/repository/starlark/StarlarkPath.java",
    "src/main/java/com/google/devtools/build/lib/bazel/repository/starlark/StarlarkExecutionResult.java",
    "src/main/java/com/google/devtools/build/lib/bazel/repository/RepositoryFetchFunction.java",
    "src/main/java/com/google/devtools/build/lib/bazel/repository/DigestWriter.java",
    "src/main/java/com/google/devtools/build/lib/bazel/repository/RepositoryUtils.java",
    "src/main/java/com/google/devtools/build/lib/bazel/repository/RepoDefinition.java",
    "src/main/java/com/google/devtools/build/lib/bazel/bzlmod/IndexRegistry.java",

    # =================================================================================
    # Bzlmod registry, module file resolution, lockfile integrity and vendoring
    # =================================================================================
    "src/main/java/com/google/devtools/build/lib/bazel/bzlmod/RegistryFactoryImpl.java",
    "src/main/java/com/google/devtools/build/lib/bazel/bzlmod/RegistryFunction.java",
    "src/main/java/com/google/devtools/build/lib/bazel/bzlmod/ArchiveRepoSpecBuilder.java",
    "src/main/java/com/google/devtools/build/lib/bazel/bzlmod/GitRepoSpecBuilder.java",
    "src/main/java/com/google/devtools/build/lib/bazel/bzlmod/BazelLockFileFunction.java",
    "src/main/java/com/google/devtools/build/lib/bazel/bzlmod/BazelLockFileModule.java",
    "src/main/java/com/google/devtools/build/lib/bazel/bzlmod/BazelLockFileValue.java",
    "src/main/java/com/google/devtools/build/lib/bazel/bzlmod/LockFileModuleExtension.java",
    "src/main/java/com/google/devtools/build/lib/bazel/bzlmod/ModuleFileFunction.java",
    "src/main/java/com/google/devtools/build/lib/bazel/bzlmod/ModuleFileGlobals.java",
    "src/main/java/com/google/devtools/build/lib/bazel/bzlmod/CompiledModuleFile.java",
    "src/main/java/com/google/devtools/build/lib/bazel/bzlmod/Discovery.java",
    "src/main/java/com/google/devtools/build/lib/bazel/bzlmod/Selection.java",
    "src/main/java/com/google/devtools/build/lib/bazel/bzlmod/BazelModuleResolutionFunction.java",
    "src/main/java/com/google/devtools/build/lib/bazel/bzlmod/BazelDepGraphFunction.java",
    "src/main/java/com/google/devtools/build/lib/bazel/bzlmod/SingleExtensionEvalFunction.java",
    "src/main/java/com/google/devtools/build/lib/bazel/bzlmod/RegularRunnableExtension.java",
    "src/main/java/com/google/devtools/build/lib/bazel/bzlmod/InnateRunnableExtension.java",
    "src/main/java/com/google/devtools/build/lib/bazel/bzlmod/ModuleExtensionEvalStarlarkThreadContext.java",
    "src/main/java/com/google/devtools/build/lib/bazel/bzlmod/YankedVersionsFunction.java",
    "src/main/java/com/google/devtools/build/lib/bazel/bzlmod/YankedVersionsUtil.java",
    "src/main/java/com/google/devtools/build/lib/bazel/bzlmod/Version.java",
    "src/main/java/com/google/devtools/build/lib/bazel/bzlmod/ModuleKey.java",
    "src/main/java/com/google/devtools/build/lib/bazel/bzlmod/RepoSpecFunction.java",
    "src/main/java/com/google/devtools/build/lib/bazel/bzlmod/VendorManager.java",
    "src/main/java/com/google/devtools/build/lib/bazel/bzlmod/VendorFileFunction.java",
    "src/main/java/com/google/devtools/build/lib/bazel/bzlmod/AttributeValues.java",
    "src/main/java/com/google/devtools/build/lib/bazel/bzlmod/TypeCheckedTag.java",
    "src/main/java/com/google/devtools/build/lib/authandtls/Netrc.java",

    # =================================================================================
    # Credentials: netrc, credential helpers and per-host scoping
    # =================================================================================
    "src/main/java/com/google/devtools/build/lib/authandtls/NetrcParser.java",
    "src/main/java/com/google/devtools/build/lib/authandtls/NetrcCredentials.java",
    "src/main/java/com/google/devtools/build/lib/authandtls/StaticCredentials.java",
    "src/main/java/com/google/devtools/build/lib/authandtls/GoogleAuthUtils.java",
    "src/main/java/com/google/devtools/build/lib/authandtls/BasicHttpAuthenticationEncoder.java",
    "src/main/java/com/google/devtools/build/lib/authandtls/credentialhelper/CredentialHelper.java",
    "src/main/java/com/google/devtools/build/lib/authandtls/credentialhelper/CredentialHelperProvider.java",
    "src/main/java/com/google/devtools/build/lib/authandtls/credentialhelper/CredentialHelperCredentials.java",
    "src/main/java/com/google/devtools/build/lib/authandtls/credentialhelper/CredentialModule.java",
    "src/main/java/com/google/devtools/build/lib/actions/ActionCacheChecker.java",
    "src/main/java/com/google/devtools/build/lib/actions/cache/CompactPersistentActionCache.java",
    "src/main/java/com/google/devtools/build/lib/actions/cache/MetadataDigestUtils.java",

    # =================================================================================
    # Local action cache and action key computation
    # =================================================================================
    "src/main/java/com/google/devtools/build/lib/actions/cache/PersistentStringIndexer.java",
    "src/main/java/com/google/devtools/build/lib/actions/ActionKeyContext.java",
    "src/main/java/com/google/devtools/build/lib/actions/AbstractAction.java",
    "src/main/java/com/google/devtools/build/lib/actions/Artifact.java",
    "src/main/java/com/google/devtools/build/lib/actions/ArtifactFactory.java",
    "src/main/java/com/google/devtools/build/lib/actions/ActionInputMap.java",
    "src/main/java/com/google/devtools/build/lib/remote/RemoteExecutionService.java",
    "src/main/java/com/google/devtools/build/lib/remote/RemoteSpawnRunner.java",
    "src/main/java/com/google/devtools/build/lib/remote/RemoteSpawnCache.java",

    # =================================================================================
    # Remote cache and remote execution: digests, merkle trees, output materialization, scrubbing
    # =================================================================================
    "src/main/java/com/google/devtools/build/lib/remote/RemoteActionFileSystem.java",
    "src/main/java/com/google/devtools/build/lib/remote/UploadManifest.java",
    "src/main/java/com/google/devtools/build/lib/remote/Scrubber.java",
    "src/main/java/com/google/devtools/build/lib/remote/GrpcCacheClient.java",
    "src/main/java/com/google/devtools/build/lib/remote/CombinedCache.java",
    "src/main/java/com/google/devtools/build/lib/remote/Chunker.java",
    "src/main/java/com/google/devtools/build/lib/remote/AbstractActionInputPrefetcher.java",
    "src/main/java/com/google/devtools/build/lib/remote/RemoteOutputChecker.java",
    "src/main/java/com/google/devtools/build/lib/remote/merkletree/MerkleTree.java",
    "src/main/java/com/google/devtools/build/lib/remote/merkletree/MerkleTreeComputer.java",
    "src/main/java/com/google/devtools/build/lib/remote/common/RemotePathResolver.java",
    "src/main/java/com/google/devtools/build/lib/remote/util/DigestUtil.java",
    "src/main/java/com/google/devtools/build/lib/remote/disk/DiskCacheClient.java",
    "src/main/java/com/google/devtools/build/lib/remote/downloader/GrpcRemoteDownloader.java",
    "src/main/java/com/google/devtools/build/lib/remote/http/HttpCacheClient.java",
    "src/main/java/com/google/devtools/build/lib/exec/SpawnInputExpander.java",
    "src/main/java/com/google/devtools/build/lib/exec/SymlinkTreeHelper.java",
    "src/main/java/com/google/devtools/build/lib/exec/RunfilesTreeUpdater.java",

    # =================================================================================
    # Spawn input staging: runfiles trees, symlink trees, sandbox and worker exec roots
    # =================================================================================
    "src/main/java/com/google/devtools/build/lib/sandbox/SandboxHelpers.java",
    "src/main/java/com/google/devtools/build/lib/sandbox/AbstractContainerizingSandboxedSpawn.java",
    "src/main/java/com/google/devtools/build/lib/sandbox/SymlinkedSandboxedSpawn.java",
    "src/main/java/com/google/devtools/build/lib/sandbox/LinuxSandboxCommandLineBuilder.java",
    "src/main/java/com/google/devtools/build/lib/worker/WorkerExecRoot.java",
    "src/main/java/com/google/devtools/build/lib/worker/WorkerFilesHash.java",
    "src/main/java/com/google/devtools/build/lib/vfs/Path.java",
    "src/main/java/com/google/devtools/build/lib/vfs/PathFragment.java",
    "src/main/java/com/google/devtools/build/lib/vfs/FileSystemUtils.java",
    "src/main/java/com/google/devtools/build/lib/vfs/DigestUtils.java",

    # =================================================================================
    # Filesystem path handling and digest primitives used by all of the above
    # =================================================================================
    "src/main/java/com/google/devtools/build/lib/vfs/DigestHashFunction.java",
    "src/main/java/com/google/devtools/build/lib/vfs/UnixGlob.java",
]


target_scopes = [
    "Critical. An artifact served at a dependency URL is admitted under a sha256/integrity value it does not match, because Checksum parsing and canonicalization, HashInputStream/HashOutputStream finalization, RetryingInputStream reconnect-and-resume, CheckContentLengthInputStream, HttpStream, or DownloadCache/RepositoryCache key validation lets unverified or partially hashed bytes through, so every machine that builds the pinned dependency executes attacker-supplied content.",
    "Critical. A tarball, zip, 7z, ar or patch published by a third party writes or links outside the repository directory, because entry names, absolute paths, `..` segments, symlink and hardlink entries, or stripPrefix handling in ZipDecompressor, TarFunction, CompressedTarFunction, ArFunction, SevenZDecompressor, StripPrefixedPath or PatchUtil are resolved after the containment check, giving arbitrary file write into the workspace or output base and code execution on the next build even though the checksum matched.",
    "Critical. A repository rule fetching attacker-published content escapes its repository root or leaks its request, because StarlarkBaseExternalContext or StarlarkRepositoryContext download/download_and_extract/extract/symlink/file/read/patch and StarlarkPath resolve an attacker-influenced string - a URL-derived output name, archive member, patch target or watched path - outside the repo directory, or follow an attacker-chosen redirect while still carrying auth.",
    "Critical. A module version already published to a registry is mutated after the fact and still accepted, because IndexRegistry, RegistryFunction, RegistryFactoryImpl, ArchiveRepoSpecBuilder, GitRepoSpecBuilder, ModuleFileFunction or CompiledModuleFile fetch MODULE.bazel, source.json, bazel_registry.json, overlay files or patches without binding them to a verified hash, so a dependency's contents change under a pinned version.",
    "Critical. The lockfile fails to bind resolution, because BazelLockFileFunction, BazelLockFileModule, BazelLockFileValue, LockFileModuleExtension, RepoSpecFunction, DigestWriter or the YankedVersions path reuses or accepts an entry whose recorded hash, marker or extension factors no longer match the resolved RepoSpec or extension result, letting a mutated, downgraded or yanked dependency be used silently on a locked build.",
    "Critical. A module the attacker publishes is selected or mapped in place of a trusted one, because Version comparison, ModuleKey, Discovery, Selection, BazelModuleResolutionFunction, BazelDepGraphFunction, ModuleFileGlobals overrides, or ModuleExtensionEvalStarlarkThreadContext / SingleExtensionEvalFunction repo naming lets a version string, repo name or repo mapping entry shadow another module's target, so the attacker's code runs where a trusted dependency was expected.",
    "Critical. A registry, remote-cache or download secret reaches a host it was never configured for, because HttpConnector, HttpConnectorMultiplexer, HttpUtils, UrlRewriter, ProxyHelper, Netrc/NetrcParser/NetrcCredentials, BasicHttpAuthenticationEncoder, GoogleAuthUtils, CredentialHelperProvider or CredentialHelperCredentials keeps or re-attaches an Authorization header, netrc entry or helper-issued token across a cross-host redirect, URL rewrite or proxy, exfiltrating the victim's credentials.",
    "Critical. Two actions that must not share a cache entry collide, because AbstractAction.computeKey, ActionKeyContext, ActionCacheChecker, MetadataDigestUtils, CompactPersistentActionCache, PersistentStringIndexer, WorkerFilesHash, MerkleTreeComputer, DigestUtil or Scrubber omits an output-affecting input - environment, argument boundary, tool, platform property, path mapping or scrubbed field - so an action an untrusted branch can run poisons the disk or remote cache entry a trusted build later hits.",
    "Critical. Cached or remotely executed outputs are materialized outside the exec root or without digest verification, because RemoteExecutionService, RemoteActionFileSystem, AbstractActionInputPrefetcher, UploadManifest, RemotePathResolver, RemoteOutputChecker, DiskCacheClient, HttpCacheClient, GrpcCacheClient, Chunker or GrpcRemoteDownloader trusts an output path, symlink target or blob digest from an action result, letting attacker-chosen bytes land at an attacker-chosen path on the build machine.",
    "Critical/High blind spot. Untrusted dependency or cache content abuses an assumption Bazel never wrote down: a value verified in one stage and trusted as verified in a later one, a path re-resolved after the containment check that approved it, an integrity or credential-scoping rule enforced on one fetch path but not its vendored, cached, mirrored, remote-downloader or `--experimental` twin, state carried across repository, module-extension, build or server-restart boundaries that was only proven safe inside one of them, or an error/retry path that keeps partially written or partially hashed results - yielding an integrity bypass, a write outside the intended root, credential exfiltration, or cache poisoning that reaches other builds.",
]


scope_scan = [
]


def question_generator(target_file: str) -> str:
    """
    Generate exploit-focused audit and fuzzing questions for one bazel target.

    ```
    target_file format:
    "'File Name: src/main/java/com/google/devtools/build/lib/bazel/repository/decompressor/ZipDecompressor.java -> Scope: Critical. ...'"
    """

    prompt = f"""
    ```

    Generate exploit-focused security audit questions for this exact bazel target:

    {target_file}

    Project focus:
    bazel is a build system. Focus only on supply-chain integrity: fetching and verifying external artifacts, archive extraction and patching, repository rules, Bzlmod registries and the lockfile, credential scoping, action-cache and remote-cache keying, and materialization of cached or remotely executed outputs.

    Rules:
    * Treat `File Name:` as the exact file/class.
    * Treat `Scope:` as the ONLY impact to target.
    * Assume full repo context is accessible.
    * Do not ask for code or say anything is missing.
    * Use exact Java symbols (class, method, field) when possible.
    * Attacker is unprivileged only: an outsider who controls content a victim's build consumes - bytes served at a dependency URL, an archive or module version they publish to a registry, a cache entry keyed by an action they can influence, or files on an untrusted branch that CI builds.
    * Attacker is NOT the victim, a maintainer, a CI operator, or a local user. They have no access to the victim's machine, output base, workspace, credentials, or the root repository's trusted BUILD/.bzl files.
    * A hostile origin server, mirror or registry counts ONLY where an integrity check is supposed to protect the build - a declared sha256/integrity, a lockfile hash, a cache key, a registry-recorded digest - and that check is shown to fail. Never assume a bare MITM, a malicious peer or node, or a malicious remote-execution/cache server as the whole finding.
    * Out of scope, never ask about: running untrusted root-repo BUILD/.bzl/WORKSPACE code (that is code execution by design), the sandbox treated as a security boundary, denial of service or resource exhaustion, anything needing local machine access, third-party dependency CVEs, best practices.
    * Ignore test files, mocks, fuzz harnesses, benches, docs, generated code, and BUILD/.bzl/TOML/config-only findings.
    * Every question must describe content an attacker actually publishes or serves. No generic unbounded-allocation, memory-growth, or "what if the input is huge" speculation without a concrete artifact and a concrete broken invariant.
    * Generate 40 to 80 high-signal questions.
    * At least 70% must target an integrity bypass of a declared checksum or lockfile hash, a file write or read outside the repository / exec root / output base, credential exfiltration to an attacker-controlled host, or cache poisoning that serves attacker content to another build.
    * Every question must be testable by a JUnit unit test, a BuildIntegrationTestCase, or a shell integration test under src/test/shell/bazel.
    * Avoid generic checklist questions and repeated root causes.

    Core invariants:
    * Integrity is binding: content admitted under a declared sha256/integrity, lockfile hash, or registry-recorded digest is byte-identical to what was pinned, and any mismatch fails the build instead of being cached or reused.
    * Containment holds: fetching, extraction, patching and output materialization write and link only under the intended repository directory, exec root, or output base, whatever an entry name, symlink target, prefix strip, or path mapping says.
    * Credentials are host-scoped: a netrc entry, Authorization header, or helper-issued token is sent only to the host it was configured for, and never survives a redirect, rewrite, or proxy to another origin.
    * Cache keys are total: two actions share a disk, remote, or worker cache entry only when every input that can change their outputs is in the key.
    * Untrusted content stays data: bytes from a dependency, a cache entry, or an untrusted branch never become commands, paths, or repository identities the build did not declare.

    Each question must include:
    1. target class/method;
    2. attacker action (concrete published content: archive entries, URLs, registry JSON, module file, action result);
    3. preconditions (what the victim's build declares and fetches);
    4. execution sequence;
    5. invariant tested;
    6. scoped impact;
    7. proof idea.

    Output only valid Python. No markdown. No explanations.

    questions = [
    "[File: {target_file}] [Function: class_or_method] Can an unprivileged ATTACKER_ACTION under PRECONDITIONS trigger EXECUTION_SEQUENCE, violating INVARIANT, causing scoped impact: SCOPE_IMPACT? Proof idea: JUnit/integration test PARAMETERS and assert INTEGRITY_BINDING, CONTAINMENT, CREDENTIAL_SCOPING, CACHE_KEY_TOTALITY, or CONTENT_STAYS_DATA.",
    ]
    """
    return prompt


def audit_format(security_question: str) -> str:
    """
    Generate a focused bazel exploit-validation prompt.
    """

    prompt = f"""# SECURITY AUDIT PROMPT

## Question
{security_question}

## Rules
- Use existing repo context only. Analyze only this question and scoped impact.
- Attacker is unprivileged only: an outsider who publishes or serves content a victim's build consumes - a dependency artifact, an archive, a registry module version, a cache entry, or files on an untrusted branch CI builds. No access to the victim's machine, output base, credentials, or trusted root-repo BUILD/.bzl files.
- A hostile server, mirror or registry counts only where a declared sha256/integrity, lockfile hash, cache key, or recorded digest is supposed to stop it and is shown to fail. Reject bare MITM, malicious-peer, malicious-node, and malicious remote-execution/cache-server premises.
- Reject findings that reduce to running untrusted root-repo Starlark, sandbox escape treated as a security boundary, denial of service or resource exhaustion, local machine access, dependency CVEs, or best practices.
- Reject test/mock/bench/docs/generated and BUILD/.bzl/TOML/config-only findings.
- Focus on real impact: integrity bypass of a pinned checksum or lockfile entry, write or read outside the repository / exec root / output base, credential exfiltration to an attacker-controlled host, or cache poisoning that reaches another build.

## Validate
- Trace the exact reachable path from the attacker's published content (bytes, entry names, URLs, registry JSON, action result) into the affected method.
- Check whether checksum verification, containment checks, lockfile mode, credential scoping, or existing error handling already stop it.
- Confirm the path is reachable with default flags on a current release, not only behind an experimental or explicitly unsafe option.
- Accept only concrete integrity bypass, escape outside the intended root, credential exfiltration, or cross-build cache poisoning.
- Require exact file/method support and a reproducible JUnit, BuildIntegrationTestCase, or shell integration PoC.

## Output
If valid, output exactly:

### Title
[Bug statement] - ([File: file_path])

### Summary
[2-3 sentences]

### Finding Description
[Code path, root cause, attacker-published inputs, exploit flow, and why existing checks fail]

### Impact Explanation
[Concrete scoped impact and matching category: Supply Chain Compromise, Integrity Bypass, Arbitrary File Write/Read, Credential Exfiltration, or Cache Poisoning]

### Likelihood Explanation
[Preconditions, what the victim must declare, feasibility, repeatability]

### Recommendation
[Specific fix]

### Proof of Concept
[JUnit or integration test plan with expected assertions]

If invalid, output exactly:
#NoVulnerability found for this question.

No extra text.
"""
    return prompt


def validation_format(report: str) -> str:
    """
    Generate a strict bounty-style validation prompt for bazel security claims.
    """
    prompt = f"""# VALIDATION PROMPT

## Security Claim
{report}

## Rules
- Validate only the submitted claim.
- Check SECURITY.md and Researcher.Md for scope, exclusions, and valid impact classes.
- Do not create a new vulnerability if the submitted claim is weak or invalid.
- Do not upgrade severity unless the provided evidence proves the higher impact.
- Accepted severities: Critical, High, and Medium, provided the claim proves a real integrity, containment, or confidentiality break. Reject Low, informational, hardening, and best-practice submissions.
- Reject bare MITM, malicious-peer, malicious-node, and malicious remote-execution/cache-server premises. A hostile server, mirror or registry is in scope only where a declared sha256/integrity, lockfile hash, cache key, or recorded digest should have stopped it and demonstrably does not.
- Reject anything that reduces to executing untrusted root-repo BUILD/.bzl/WORKSPACE code, since evaluating untrusted Starlark is documented as code execution by design.
- Reject sandbox-escape claims that assume the sandbox is a security boundary, denial of service, resource exhaustion, crash-only bugs, and performance issues.
- Reject if the exploit needs local machine access, an existing foothold on the victim host, maintainer or CI-operator privileges, stolen credentials, victim social engineering, or a non-default/experimental flag the victim would not set.
- Reject third-party dependency CVEs, docs/style, generated files, and test/mock/bench/BUILD/config-only issues.
- Reject if the bug was fixed, acknowledged, or publicly disclosed already, per the eligibility rules.
- A valid report must be triggerable by content an outside party publishes or serves into the victim's build, unless the claim proves escalation from that starting point.
- The final impact must map to an in-scope category: Supply Chain Compromise (attacker content executes or lands in victim outputs), Integrity Bypass (pinned checksum, lockfile, or registry digest evaded), Arbitrary File Write or Read outside the repository / exec root / output base, Credential or Secret Exfiltration to an attacker-controlled host, or Cache Poisoning that serves attacker content to another build.
- Prefer #NoVulnerability over speculative reports.

## Required Validation Checks
All must pass:
1. Exact in-scope file, class, method, and line references.
2. Clear root cause and broken security assumption.
3. Reachable exploit path: preconditions (what the victim declares and fetches) -> attacker-published content -> trigger -> bad result.
4. Existing checksum verification, containment checks, lockfile mode, credential scoping, and error handling reviewed and shown insufficient.
5. Concrete in-scope impact with realistic likelihood and correct severity.
6. Reproducible proof path: JUnit PoC, BuildIntegrationTestCase, or shell integration test under src/test/shell/bazel.
7. No obvious rejection reason from SECURITY.md, known issues, privilege assumptions, or scope exclusions.

## Silent Triage Questions
Before output, internally answer:
- Can an outsider who only publishes or serves content trigger this, without access to the victim's machine or credentials?
- Does the code actually behave as claimed with default flags on a current release?
- Is the impact caused by this code, not by the victim choosing to build untrusted Starlark?
- Is the integrity bypass, escape, credential leak, or poisoning concrete rather than hypothetical?
- Would a Bazel security triager accept the proof-of-concept?
- What exact test would prove it?

## Output
If valid, output exactly:

Audit Report

## Title
[Clear vulnerability statement] - ([File: file_path])

## Summary
[2-3 sentence summary of the bug and impact]

## Finding Description
[Exact code path, root cause, exploit flow, and why existing checks fail]

## Impact Explanation
[Concrete in-scope impact, severity rationale, and bounty category]

## Likelihood Explanation
[Attacker capability, what the victim must declare, feasibility, repeatability]

## Recommendation
[Specific fix guidance]

## Proof of Concept
[Minimal reproducible steps or JUnit/integration test plan]

If invalid, output exactly:
#NoVulnerability found for this question.

Output only one of the two outcomes above. No extra text.
"""
    return prompt


def scan_format(report: str) -> str:
    """
    Generate a short cross-project analog scan prompt for bazel.
    """
    prompt = f"""# ANALOG SCAN PROMPT

## External Report
{report}

## Rules
- Use in-scope production repo context only. Do not ask for code or claim missing files.
- Use the external report only as a bug-class hint, not as proof.
- Keep only analogs an outside content publisher can reach: artifact download and checksum verification, archive extraction and patching, repository rules, Bzlmod registries and the lockfile, credential scoping, action/remote/worker cache keying, or materialization of cached and remotely executed outputs.
- Reject bare MITM, malicious-peer, malicious-node, malicious remote-server, untrusted-root-Starlark, sandbox-boundary, denial-of-service, local-access, dependency-only, mocked-only, and no-impact analogs.

## Validate
- Map the bug class to the strongest reachable bazel path from content an attacker publishes or serves.
- Prove root cause with exact file/method support.
- Accept only concrete integrity bypass of a pinned checksum or lockfile entry, write or read outside the repository / exec root / output base, credential exfiltration to an attacker-controlled host, or cache poisoning that reaches another build.

## Output (Strict)
If valid analog exists, output:

### Title
[Clear vulnerability statement] - ([File: file_path])

### Summary
### Finding Description
### Impact Explanation
### Likelihood Explanation
### Recommendation
### Proof of Concept

If not, output exactly:
#NoVulnerability found for this question.

No extra text.
"""
    return prompt
