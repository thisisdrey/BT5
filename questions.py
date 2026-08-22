import json
import os

from decouple import config

# todo: if scope_files is: 500 > 50, 300 > 30 , 100 > 10
MAX_REPO = 22
# todo: the GitLab namespace/project path, for example group/project
SOURCE_REPO = 'gitlab-org/gitaly'
# todo: the name of the repository
REPO_NAME = 'gitaly'

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
    # Storage path resolution: relative-path validation, storage escape, repo location
    # =================================================================================
    "internal/gitaly/storage/repository_path.go",
    "internal/gitaly/storage/path_helpers.go",
    "internal/gitaly/storage/locator.go",
    "internal/gitaly/storage/storage.go",
    "internal/gitaly/config/locator.go",
    "internal/gitaly/storage/walk_directory.go",
    "internal/gitaly/storage/fs.go",
    "internal/tempdir/tempdir.go",

    # =================================================================================
    # Git argument construction: flag injection, config injection, execution environment
    # =================================================================================
    "internal/git/gitcmd/command_options.go",
    "internal/git/gitcmd/command_description.go",
    "internal/git/gitcmd/command_factory.go",
    "internal/git/gitcmd/command.go",
    "internal/git/gitcmd/staticargs.go",
    "internal/git/gitcmd/ssh.go",
    "internal/git/gitcmd/protocol.go",
    "internal/git/revision.go",
    "internal/git/object_id.go",
    "internal/git/reference.go",
    "internal/git/repository.go",

    # =================================================================================
    # Hook execution and payload trust: pre-receive/update gating, custom hooks
    # =================================================================================
    "internal/git/gitcmd/hooks_payload.go",
    "internal/git/gitcmd/hooks_options.go",
    "internal/gitaly/hook/manager.go",
    "internal/gitaly/hook/prereceive.go",
    "internal/gitaly/hook/update.go",
    "internal/gitaly/hook/postreceive.go",
    "internal/gitaly/hook/custom.go",
    "internal/gitaly/hook/procreceive_handler.go",
    "internal/gitaly/service/operations/update_with_hooks.go",

    # =================================================================================
    # Object isolation: quarantine, alternates, object pools, cross-repository leakage
    # =================================================================================
    "internal/git/quarantine/quarantine.go",
    "internal/git/alternates/alternates.go",
    "internal/gitaly/service/objectpool/link.go",
    "internal/gitaly/service/objectpool/alternates.go",
    "internal/gitaly/service/objectpool/create.go",
    "internal/gitaly/service/objectpool/fetch_into_object_pool.go",
    "internal/gitaly/service/objectpool/util.go",
    "internal/git/objectpool/pool.go",

    # =================================================================================
    # Unauthenticated/low-privilege RPC entry points: pack protocol and transfer
    # =================================================================================
    "internal/gitaly/service/smarthttp/upload_pack.go",
    "internal/gitaly/service/smarthttp/receive_pack.go",
    "internal/gitaly/service/smarthttp/inforefs.go",
    "internal/gitaly/service/ssh/upload_pack.go",
    "internal/gitaly/service/ssh/receive_pack.go",
    "internal/gitaly/service/ssh/upload_archive.go",
    "internal/gitaly/service/ssh/upload_command.go",
    "internal/git/pktline/pktline.go",

    # =================================================================================
    # Attacker-supplied remotes and streams: SSRF, credential leakage, untrusted archives
    # =================================================================================
    "internal/gitaly/service/repository/create_repository_from_url.go",
    "internal/gitaly/service/repository/create_repository_from_snapshot.go",
    "internal/gitaly/service/repository/create_repository_from_bundle.go",
    "internal/gitaly/service/repository/fetch_remote.go",
    "internal/gitaly/service/repository/fetch_bundle.go",
    "internal/gitaly/service/repository/replicate.go",
    "internal/gitaly/service/repository/create_fork.go",
    "internal/gitaly/service/repository/set_custom_hooks.go",
    "internal/gitaly/service/repository/get_custom_hooks.go",
    "internal/gitaly/service/repository/restore_repository.go",
    "internal/gitaly/repoutil/custom_hooks.go",
    "internal/gitaly/repoutil/create.go",
    "internal/bundleuri/git_config.go",
    "internal/bundleuri/sink.go",

    # =================================================================================
    # Path-scoped read RPCs and archiving: traversal and unintended file disclosure
    # =================================================================================
    "internal/gitaly/service/repository/archive.go",
    "internal/gitaly/service/repository/snapshot.go",
    "internal/gitaly/service/repository/search_files.go",
    "internal/gitaly/service/repository/info_attributes.go",
    "internal/gitaly/service/repository/file_attributes.go",
    "internal/gitaly/service/commit/tree_entry.go",
    "internal/gitaly/service/commit/get_tree_entries.go",
    "internal/gitaly/service/commit/raw_blame.go",
    "internal/gitaly/service/blob/get_blob.go",
    "internal/gitaly/service/blob/lfs_pointers.go",
    "internal/archive/archive.go",
    "internal/archive/match_walker.go",
    "internal/archive/tar_builder.go",

    # =================================================================================
    # Reference mutation: ref-name validation, force updates, transactional ref writes
    # =================================================================================
    "internal/gitaly/service/repository/write_ref.go",
    "internal/gitaly/service/ref/update_references.go",
    "internal/gitaly/service/ref/delete_refs.go",
    "internal/gitaly/service/operations/user_create_branch.go",
    "internal/gitaly/service/operations/commit_files.go",
    "internal/gitaly/service/operations/apply_patch.go",
    "internal/gitaly/service/operations/submodules.go",
    "internal/gitaly/service/operations/tags.go",
    "internal/git/updateref/updateref.go",

    # =================================================================================
    # Transport authentication and request-scoped parsing shared by every RPC
    # =================================================================================
    "auth/token.go",
    "internal/grpc/middleware/requestinfohandler/requestinfohandler.go",
    "internal/grpc/middleware/panichandler/panic_handler.go",
    "internal/grpc/middleware/limithandler/middleware.go",
    "internal/grpc/sidechannel/sidechannel.go",
    "internal/grpc/backchannel/backchannel.go",
    "internal/git/catfile/parser.go",
    "internal/git/gitattributes/check_attr.go",
    "internal/git/lfs.go",
    "internal/helper/security.go",
    "internal/gitlab/http_client.go",
]


target_scopes = [
    "Critical. An unprivileged attacker who only controls gRPC request fields (`Repository.relative_path`, `storage_name`, object-pool paths) reads or writes files outside the configured storage root, by defeating `storage.ValidateRelativePath`, the path joining in `config.Locator`/`storage.Locator`, or the pool-path checks in the objectpool service, reaching another tenant's repository, custom hooks, or arbitrary host files.",
    "Critical. An unprivileged attacker achieves command or option injection into a spawned git process, by smuggling a leading `-`, `--upload-pack=`, `--output=`, `ext::`, or `-c` payload through a revision, ref name, path, or remote URL that `git.ValidateRevision`, `gitcmd.Command`/`command_options.go`, or `gitcmd.commandDescriptions` fails to reject, causing Gitaly to execute attacker-chosen code or read attacker-chosen files.",
    "Critical. An unprivileged attacker reads objects of a repository they have no access to, by abusing alternates/quarantine/object-pool wiring — `alternates.go`, `quarantine.NewQuarantine`, `Link`/`DisconnectGitAlternates`, or `remoterepo` — so that an RPC on a repository they do control resolves or serves objects belonging to a private repository of another user.",
    "Critical. An unprivileged pusher gets refs updated with objects that never passed verification, by bypassing hook invocation or the quarantine boundary in `hook.PreReceive`/`hook.Update`, `updateref`, `updateReferenceWithHooks`, or by forging/replaying the hooks payload in `gitcmd.HooksPayload`, so unvetted objects become reachable or a protected-branch/access decision is skipped.",
    "Advanced. An unprivileged attacker escapes the repository directory while Gitaly writes attacker-supplied archive or snapshot data, by crafting tar entries, symlinks, or `..` paths accepted by `SetCustomHooks`/`repoutil.ExtractHooks`, `CreateRepositoryFromSnapshot`, `RestoreRepository`, or `internal/archive`, planting or overwriting files (including hooks) outside the target repository.",
    "Advanced. An unprivileged attacker turns a repository RPC into an SSRF or credential-disclosure primitive, by supplying a crafted remote URL, redirect target, HTTP header, or bundle-URI location to `CreateRepositoryFromURL`, `FetchRemote`, `FetchBundle`, `CreateRepositoryFromSnapshot`, or `bundleuri`, making Gitaly reach an internal endpoint or emit the configured credentials/auth header to an attacker-controlled host.",
    "Advanced. An unprivileged attacker bypasses Gitaly's transport authentication or request scoping, by defeating the HMAC/timestamp checks in `gitalyauth` (`token.go`) — replay outside `tokenValidityDuration`, signature-comparison weakness, or a version-downgrade to the v1 path — and issues RPCs against repositories on the server without holding the shared token.",
    "Advanced. A remote attacker with only push/pull-level access exhausts Gitaly or crashes an RPC handler on default configuration, by sending a crafted pack, pktline stream, git object, `.gitattributes`, or LFS pointer that drives unbounded memory/CPU/disk in `pktline`, `catfile`, `gitattributes.CheckAttr`, `lfs.go`, or triggers a panic that `panichandler` converts into a persistent failure of the process or partition.",
    "Intermediate. An unprivileged attacker obtains secrets or another tenant's metadata from Gitaly's error and log surfaces, by crafting a remote URL, ref, or path whose credentials or absolute storage path survive `helper.SanitizeString`/`SanitizeError`, `structerr` formatting, or `requestinfohandler` field extraction and are returned in a gRPC error the attacker can read.",
]


scope_scan = [
]


def question_generator(target_file: str) -> str:
    """
    Generate exploit-focused audit and fuzzing questions for one gitaly target.

    ```
    target_file format:
    "'File Name: internal/gitaly/storage/repository_path.go -> Scope: Critical. ...'"
    """

    prompt = f"""
    ```

    Generate exploit-focused security audit and fuzzing questions for this exact gitaly target:

    {target_file}

    Project focus:
    Gitaly is the gRPC server that GitLab uses to run Git close to disk. Every request field a user
    can influence - repository relative path, storage name, revisions, ref names, file paths, remote
    URLs, pushed pack data, tar/bundle streams - reaches path resolution, a spawned git process, hook
    execution, or object storage. Focus on storage-path escape, git flag/config injection, quarantine
    and object-pool isolation, hook bypass on push, untrusted archive extraction, SSRF and credential
    leakage on remote fetches, transport auth, and DoS in an RPC handler.

    Rules:
    * Treat `File Name:` as the exact file/module.
    * Treat `Scope:` as the ONLY impact to target.
    * Assume full repo context is accessible.
    * Do not ask for code or say anything is missing.
    * Use exact Go symbols (function, method, struct, interface, constant, config field) when possible.
    * Attacker is unprivileged only: a GitLab user with no special role who can push/fetch, fork or
      import a repository they own, and thereby cause Gitaly RPCs to run with attacker-chosen fields
      and attacker-chosen repository content.
    * Attacker is NOT an instance admin, operator, Gitaly/Praefect node, or anyone with shell, disk,
      or config access; does NOT hold the shared auth token or any leaked secret; and cannot rely on
      a malicious peer/replica, MITM, local-network, or social engineering.
    * Ignore test files, mocks, fixtures, testhelper/gittest, docs, generated protobuf, build/CI/config.
    * Ignore self-harm (attacker damaging only their own repository) and pure best-practice critique.
    * Generate 12 to 16 high-signal questions.
    * At least 70% must target storage-path escape, git argument/config injection, cross-repository
      object access, hook or quarantine bypass, archive/tar extraction escape, SSRF or credential
      leak, auth bypass, or DoS of an RPC handler.
    * Every question must be testable by a Go test, a crafted gRPC request, a crafted push/pack or
      pktline stream, a malicious repository or tar/bundle payload, or a fuzz/differential test over
      encoded inputs.
    * Avoid generic checklist questions and repeated root causes.

    Core invariants:
    * Paths stay inside the storage: every path derived from a request resolves under the configured
      storage root for that exact repository, after `storage.ValidateRelativePath` and locator joining,
      with no traversal, symlink escape, or absolute-path override.
    * Git is never re-configured by input: user-controlled revisions, refs, paths and URLs are passed
      as operands, never interpretable as options, `-c` config, or transport helpers.
    * Repositories are isolated: objects, alternates, quarantine directories and pools serve only the
      repository the request is authorized for; unvetted objects stay quarantined until hooks pass.
    * Writes are gated: reference updates on a push run through pre-receive/update/post-receive with an
      unforgeable hooks payload; no path lets a ref advance while skipping them.
    * Secrets and host state stay internal: tokens, remote credentials, absolute paths and other repos'
      metadata never reach an error, log line, or response the attacker can read.

    Each question must include:
    1. target module/function;
    2. attacker action;
    3. preconditions;
    4. request/call sequence;
    5. invariant tested;
    6. scoped impact;
    7. proof idea.

    Output only valid Python. No markdown. No explanations.

    questions = [
    "[File: {target_file}] [Function: package.Function] Can an unprivileged ATTACKER_ACTION under PRECONDITIONS trigger REQUEST_SEQUENCE, violating INVARIANT, causing scoped impact: SCOPE_IMPACT? Proof idea: Go test / gRPC request / crafted push / malicious tar-bundle INPUTS and assert PATH_CONFINEMENT, ARGUMENT_SAFETY, REPO_ISOLATION, HOOK_GATING, or SECRET_CONFINEMENT.",
    ]
    """
    return prompt


def audit_format(security_question: str) -> str:
    """
    Generate a focused gitaly exploit-validation prompt.
    """

    prompt = f"""# SECURITY AUDIT PROMPT

## Question
{security_question}

## Rules
- Use existing repo context only. Analyze only this question and scoped impact.
- Attacker is unprivileged only: a GitLab user with no special role who can push/fetch, fork or import
  a repository they own, and so control RPC fields and repository content. No admin/operator role, no
  shell or disk access, no shared auth token or leaked secret, no malicious Gitaly/Praefect node, no
  MITM, local-network, or social-engineering assumptions.
- Reject anything requiring privileged access, a stolen secret, non-default configuration, a compromised
  peer/replica, or a bug in GitLab Rails/git itself rather than in Gitaly.
- Reject anything that depends only on test/mock/fixture/docs/generated/build files, a dependency bug
  alone, or best-practice cleanup without exploitable impact.
- Focus on real compromise paths: storage-path escape, git argument/config injection or command
  execution, cross-repository object access, hook or quarantine bypass, archive extraction escape,
  SSRF or credential disclosure, auth bypass, and DoS of an RPC handler.

## Validate
- Trace the exact reachable path from attacker input (RPC field, ref/revision/path, pushed pack,
  tar/bundle stream, remote URL) into the affected function.
- Check whether existing checks already stop it: `storage.ValidateRelativePath` and locator path
  joining, `git.ValidateRevision` and the `gitcmd` option/description allowlists, quarantine and
  alternates handling, hooks payload verification, `helper.SanitizeString`, and request limits.
- Account for what the attacker actually controls versus what GitLab Rails or the transaction layer
  fixes before Gitaly sees it.
- Accept only concrete impact: file read/write outside the repository, command or git-option injection,
  another repository's objects or metadata disclosed, unvetted refs accepted, secret leak, or a crash
  or resource exhaustion of a handler.
- Require exact file/function support and a reproducible Go test or RPC-level PoC.

## Output
If valid, output exactly:

### Title
[Bug statement] - ([File: file_path])

### Summary
[2-3 sentences]

### Finding Description
[Code path, root cause, attacker inputs, exploit flow, and why checks fail]

### Impact Explanation
[Concrete scoped impact and matching GitLab bounty impact class]

### Likelihood Explanation
[Preconditions, attacker capability, feasibility, repeatability]

### Recommendation
[Specific fix]

### Proof of Concept
[Go test, crafted gRPC request, push/pack sequence, or malicious tar/bundle with expected assertions]

If invalid, output exactly:
#NoVulnerability found for this question.

No extra text.
"""
    return prompt


def validation_format(report: str) -> str:
    """
    Generate a strict bounty-style validation prompt for gitaly security claims.
    """
    prompt = f"""# VALIDATION PROMPT

## Security Claim
{report}

## Rules
- Validate only the submitted claim.
- Check SECURITY.md and Researcher.Md for scope, exclusions, and valid impact classes.
- Do not create a new vulnerability if the submitted claim is weak or invalid.
- Do not upgrade severity unless the provided evidence proves the higher impact.
- Reject privileged-actor (instance admin, operator, Gitaly/Praefect node, shell or disk access),
  leaked-token, malicious-peer, physical/local-network, MITM, social-engineering, dependency-only,
  docs/style, and test/mock/generated/config-only issues.
- Reject self-harm on the attacker's own repository, missing-hardening claims, scanner output,
  pure-DDoS/volumetric claims, and theoretical claims with no demonstrated impact.
- A valid report must be triggerable by an ordinary GitLab user pushing, fetching, forking or importing
  a repository they own against Gitaly on default configuration.
- The final impact must map to an in-scope class: storage-path escape (arbitrary file read/write),
  git argument/config injection or command execution, cross-repository object or metadata access,
  hook or quarantine bypass accepting unvetted refs, archive extraction escape, SSRF or credential
  disclosure, transport auth bypass, or DoS of an RPC handler.
- Prefer #NoVulnerability over speculative reports.

## Required Validation Checks
All must pass:
1. Exact in-scope file, function, and line/code references.
2. Clear root cause and broken security assumption.
3. Reachable exploit path: preconditions -> attacker RPC/push/payload -> trigger -> bad result.
4. Existing path/revision/hook/quarantine/sanitization checks reviewed and shown insufficient.
5. Concrete in-scope impact with realistic likelihood and attacker capability.
6. Reproducible proof path: Go test, crafted gRPC call, push/pack sequence, or malicious tar/bundle.
7. No obvious rejection reason from SECURITY.md, privilege assumptions, or scope exclusions.

## Silent Triage Questions
Before output, internally answer:
- Can an ordinary unprivileged GitLab user trigger this with no admin role, no shared token, and no
  malicious node?
- Does the code actually behave as claimed on the current release version and default config?
- Is the impact caused by Gitaly's own code, not by GitLab Rails, git upstream, or a dependency alone?
- Is the escaped path, injected argument, leaked object, accepted ref, secret, or crash concrete and
  not hypothetical?
- Would a GitLab triager accept the proof?
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
[Concrete in-scope impact, severity rationale, and GitLab bounty category]

## Likelihood Explanation
[Attacker capability, required conditions, feasibility, repeatability]

## Recommendation
[Specific fix guidance]

## Proof of Concept
[Minimal reproducible RPC/push/payload sequence or Go test plan]

If invalid, output exactly:
#NoVulnerability found for this question.

Output only one of the two outcomes above. No extra text.
"""
    return prompt


def scan_format(report: str) -> str:
    """
    Generate a short cross-project analog scan prompt for gitaly.
    """
    prompt = f"""# ANALOG SCAN PROMPT

## External Report
{report}

## Rules
- Use in-scope production repo context only. Do not ask for code or claim missing files.
- Use the external report only as a bug-class hint, not as proof.
- Keep only unprivileged analogs in storage-path resolution, git command/argument construction, hook
  and quarantine gating, object-pool and alternates isolation, archive or bundle extraction, remote
  URL handling, transport auth, or RPC-handler resource limits.
- Reject privileged-actor, leaked-token, malicious-peer/node, MITM, dependency-only, test-only, and
  no-impact analogs.

## Validate
- Map the bug class to the strongest reachable gitaly path from an ordinary user's push, fetch, fork,
  import, or crafted RPC field.
- Prove root cause with exact file/module/function support.
- Accept only concrete storage escape, argument/config injection, cross-repository object access, hook
  or quarantine bypass, extraction escape, SSRF or credential disclosure, auth bypass, or DoS of a handler.

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
