### Title
Attacker-controlled "git" executable in a cloned repository can be run in place of the system Git binary during `git_repository` fetch on Windows - (File: `tools/build_defs/repo/git_worker.bzl`)

### Summary
`git_repository`/`new_git_repository` fetch logic (`tools/build_defs/repo/git_worker.bzl`) always invokes the unqualified command name `"git"` (e.g. `start = ["git", "-c", "core.fsmonitor=false"]`) and explicitly sets `working_directory = str(git_repo.directory)` — the very checkout directory that is populated with attacker-controlled content from the remote being fetched. [1](#0-0) 

### Finding Description
Every git subcommand in this file (`init`, `add_origin`, `fetch`, `reset`, `clean`, `update_submodules`, sparse-checkout config, `_get_head_commit`, `_get_head_date`) is dispatched through `_execute`, which builds the argv as `["git", "-c", "core.fsmonitor=false", ...]` and runs it with `working_directory = str(git_repo.directory)`. [2](#0-1) [3](#0-2) 

This is structurally the same bug class as CVE-2021-21237 (and its predecessor CVE-2020-27955): on Windows, when a process is launched with an unqualified executable name and no explicit directory separator, the OS process-creation search order includes the current/working directory before `PATH`. If a hostile Git remote's tree contains a file named `git.exe` or `git.bat` at the repository root, then after the initial `git init`/`git clone` step populates `git_repo.directory` with that file (fetch happens before `reset`/`clean`/`submodule update`/log commands run in that same directory), every *subsequent* `ctx.execute(["git", ...], working_directory=git_repo.directory)` call risks resolving to the attacker's dropped `git.exe`/`git.bat` instead of the real system Git, because the working directory is inside attacker-supplied content and the command is unqualified.

This differs from a bare "malicious peer" premise because the attacker doesn't need any privileged position — they only need to be the origin of a `git_repository` `remote` URL that a victim's `MODULE.bazel`/`WORKSPACE` fetches (a common untrusted-dependency scenario), i.e., exactly the "attacker publishes content a victim's build consumes" pattern in scope. `git_repository` has no `sha256`/checksum verification of the fetched tree (unlike `http_archive`), so there is no integrity gate that would need to be bypassed — the content is trusted implicitly once fetched, and the executable-resolution ambiguity is what breaks containment.

I was not able to conclusively verify in this pass whether Bazel's actual Windows process-launch path (native code in `src/main/native/windows/process.cc` / `processes-jni.cc`, which backs `ctx.execute()`) uses a raw `CreateProcess`-style invocation with an unqualified `lpCommandLine` (which triggers the vulnerable Windows search order that includes the current directory), or whether it pre-resolves `"git"` to an absolute path via `PATH` lookup before invoking `CreateProcess` (which would mean the argv[0] passed to `CreateProcess` is already absolute, closing this particular gap). This is the same class of ambiguity that caused the original Go `exec.Command` bug in git-lfs, and it needs to be checked in the C++ implementation before this can be treated as a confirmed exploitable path rather than a plausible analog.

### Impact Explanation
If exploitable, this results in arbitrary code execution on the developer's/CI's machine at build/fetch time, driven entirely by the contents of an attacker-controlled Git remote referenced by `git_repository`/`new_git_repository`. This is a supply-chain code-execution primitive equivalent in severity to the original CVE.

### Likelihood Explanation
Requires: (1) target is running on Windows, (2) target's build depends on a `git_repository`/`new_git_repository` pointing at an attacker-influenced remote or branch (a realistic scenario for third-party/forked dependencies or CI building untrusted PR branches), and (3) Bazel's native process launch on Windows indeed performs current-directory-inclusive unqualified-name resolution rather than pre-resolving `git` via `PATH` to an absolute path. Point (3) is unconfirmed with the tools available in this session.

### Recommendation
- In `tools/build_defs/repo/git_worker.bzl`, resolve the `git` binary to an absolute path once (e.g., via `which git`/`ctx.which("git")`) before entering the checkout directory, and pass the absolute path as argv[0] to every `ctx.execute` call, rather than the bare string `"git"`.
- Verify/harden the native Windows process-launch code path (`src/main/native/windows/process.cc`, `processes-jni.cc`) so that unqualified command names are resolved via `PATH` search using `SearchPathW` semantics that exclude the CWD, matching Go's post-CVE-2021-21237 fix, rather than relying on `CreateProcess`'s default (vulnerable) search order.

### Proof of Concept
Conceptual repro (would need to be turned into a `src/test/shell/bazel` shell test on a Windows runner):
1. Create a Git repository whose tree contains an executable `git.bat` at its root that writes a marker file / exfiltrates data, and a legitimate small history.
2. Define `git_repository(name="pwn", remote="<path-or-url-to-that-repo>", commit="<sha>")` in `MODULE.bazel`.
3. Run `bazel fetch @pwn//:all` on Windows.
4. Observe whether the marker file is created / attacker code runs during any of the `_execute` calls in `git_worker.bzl` that run after the tree is populated and while `working_directory` is set to the checkout directory (i.e., `reset`, `clean`, `update_submodules`, `_get_head_commit`, `_get_head_date`, sparse-checkout config). [4](#0-3)

### Citations

**File:** tools/build_defs/repo/git_worker.bzl (L152-159)
```text
def init(ctx, git_repo):
    cl = ["git", "init", str(git_repo.directory)]
    st = ctx.execute(cl, environment = ctx.os.environ | _GIT_LOCAL_ENV_VARS)
    if st.return_code != 0:
        _error(ctx.name, cl, st.stderr)

def add_origin(ctx, git_repo, remote):
    _git(ctx, git_repo, "remote", "add", "origin", remote)
```

**File:** tools/build_defs/repo/git_worker.bzl (L204-239)
```text
def reset(ctx, git_repo):
    _git(ctx, git_repo, "reset", "--hard", git_repo.reset_ref)

def clean(ctx, git_repo):
    _git(ctx, git_repo, "clean", "-xdf")

def update_submodules(ctx, git_repo, recursive = False):
    if recursive:
        # "protocol.file.allow=always" allows the submodule command clone from a local directory.
        # It's necessary for Git 2.38.1 and assoicated backport versions.
        # See https://github.com/bazelbuild/bazel/issues/17040
        _git_maybe_shallow(ctx, git_repo, "-c", "protocol.file.allow=always", "submodule", "update", "--init", "--recursive", "--checkout", "--force")
    else:
        _git_maybe_shallow(ctx, git_repo, "-c", "protocol.file.allow=always", "submodule", "update", "--init", "--checkout", "--force")

def _get_head_commit(ctx, git_repo):
    return _git(ctx, git_repo, "log", "-n", "1", "--pretty=format:%H")

def _get_head_date(ctx, git_repo):
    return _git(ctx, git_repo, "log", "-n", "1", "--pretty=format:%cd", "--date=raw")

def _git(ctx, git_repo, command, *args):
    start = [command]
    st = _execute(ctx, git_repo, start + list(args))
    if st.return_code != 0:
        _error(ctx.name, ["git"] + start + list(args), st.stderr)
    return st.stdout

def _git_maybe_shallow(ctx, git_repo, command, *args):
    start = [command]
    args_list = list(args)
    if git_repo.shallow:
        st = _execute(ctx, git_repo, start + [git_repo.shallow] + args_list)
        if st.return_code == 0:
            return st
    return _execute(ctx, git_repo, start + args_list)
```

**File:** tools/build_defs/repo/git_worker.bzl (L311-319)
```text
def _execute(ctx, git_repo, args):
    # "core.fsmonitor=false" disables git from spawning a file system monitor which can cause hangs when cloning a lot.
    # See https://github.com/bazelbuild/bazel/issues/21438
    start = ["git", "-c", "core.fsmonitor=false"]
    return ctx.execute(
        start + args,
        environment = ctx.os.environ | _GIT_LOCAL_ENV_VARS,
        working_directory = str(git_repo.directory),
    )
```
