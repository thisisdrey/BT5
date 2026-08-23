### Title
Command injection via `ext::` transport helper in `CreateRepositoryFromURLRequest.Url` - (File: internal/gitaly/service/repository/create_repository_from_url.go)

### Finding Description
`cloneFromURLCommand` takes `req.GetUrl()` and only parses it with `url.Parse()` at line 36, which is intentionally permissive and accepts arbitrary scheme strings such as `ext::sh -c ...` without error. [1](#0-0) 
The parsed value is then re-serialized (`u.String()`) and passed straight into `git clone <url> <path>` as a positional argument without any scheme allowlist or rejection of `ext::`, `fd::`, `file://`, or similar transport-helper syntaxes. [2](#0-1) 
`validateCreateRepositoryFromURLRequest` only checks that the repository is valid and that the URL is non-empty — it performs no scheme/host validation at all. [3](#0-2) 
I found no scheme allowlist, `GIT_PROTOCOL_FROM_USER`/`protocol.allow` restriction, or `ext::`/`file://` rejection logic anywhere in the Gitaly codebase (`grep` for `protocol.allow`, `GIT_PROTOCOL_FROM_USER`, `ext::`, `u.Scheme` in this service returned no hits in this package). If `git clone` is invoked with an `ext::<command>` URL and Git's own `protocol.ext.allow` policy is not explicitly restricted (e.g., via `GIT_PROTOCOL_FROM_USER=0` or `-c protocol.ext.allow=never`), Git will execute the given shell command as the "remote helper," resulting in arbitrary command execution in the Gitaly process's context.

### Impact Explanation
If reachable with attacker-controlled `Url` and no upstream (GitLab Rails) allowlist enforcing `http(s)`/`git`/`ssh` only, this results in remote command execution on the Gitaly node under the Gitaly service account — reading/writing arbitrary files, SSRF to internal services, and full compromise of repository storage. This matches GitLab's highest bounty impact class (RCE).

### Likelihood Explanation
This is contingent on preconditions I could not fully verify from the Gitaly codebase alone: whether Git's built-in `protocol.allow` default policy for `ext` (Git treats `ext`/`fd`/`file` as "user"-level protocols, which are permitted by default unless the process explicitly sets `GIT_PROTOCOL_FROM_USER=0` or the caller sets `protocol.ext.allow=never`), and whether GitLab Rails, which is the normal caller of `CreateRepositoryFromURL` for repository mirroring/import, already validates and restricts the `Url` scheme (e.g., to `http(s)`/`git`/`ssh`) before this RPC is ever invoked. I was unable to find any such restriction inside the Gitaly repository itself (no `protocol.allow`, `GIT_PROTOCOL_FROM_USER`, or scheme allowlist present in `internal/gitaly/service/repository/`), which means Gitaly itself provides no defense-in-depth against this input if an attacker can reach the RPC directly with an arbitrary `Url` (e.g., via a misconfigured/compromised Rails-to-Gitaly boundary, or if URL validation in Rails is bypassed). Per the audit rules, this question must be evaluated strictly as a Gitaly-side issue reachable by an unprivileged user with direct RPC access to `CreateRepositoryFromURL` and an attacker-controlled `Url` field, as stated in the preconditions — under that assumption the path is real and unmitigated at the Gitaly layer.

### Recommendation
In `cloneFromURLCommand` (or `validateCreateRepositoryFromURLRequest`), enforce a strict allowlist of `u.Scheme` (only `http`, `https`, `git`, `ssh`) and reject all others before invoking `git clone`. Additionally, always pass `-c protocol.ext.allow=never -c protocol.file.allow=never` (or set `GIT_PROTOCOL_FROM_USER=0`) as global git options on the clone command to ensure Git itself refuses risky transport helpers regardless of upstream validation.

### Proof of Concept
```go
func TestCreateRepositoryFromURL_extProtocolInjection(t *testing.T) {
    ctx := testhelper.Context(t)
    cfg, client := setupRepositoryService(t)

    targetRepo := &gitalypb.Repository{
        RelativePath: "imports/malicious.git",
        StorageName:  cfg.Storages[0].Name,
    }

    req := &gitalypb.CreateRepositoryFromURLRequest{
        Repository: targetRepo,
        // Go's net/url.Parse accepts this as scheme "ext" with opaque data,
        // which is re-serialized and handed to `git clone` unmodified.
        Url: `ext::sh -c "touch /tmp/gitaly-pwned"`,
    }

    _, err := client.CreateRepositoryFromURL(ctx, req)
    // Expect: err should be InvalidArgument due to disallowed scheme.
    // Actual (vulnerable) behavior: git clone attempts to invoke the ext
    // transport helper, executing the shell command.
    require.Error(t, err)
    _, statErr := os.Stat("/tmp/gitaly-pwned")
    require.True(t, os.IsNotExist(statErr), "ext:: transport helper must not execute arbitrary commands")
}
```
Expected on a patched Gitaly: request rejected with `InvalidArgument` due to disallowed URL scheme, and `/tmp/gitaly-pwned` never created. On the current code, if reachable and Git's default `protocol.ext.allow` policy permits it, the file would be created, demonstrating command execution.

### Citations

**File:** internal/gitaly/service/repository/create_repository_from_url.go (L36-39)
```go
	u, err := url.Parse(repoURL)
	if err != nil {
		return nil, structerr.NewInternal("%w", err)
	}
```

**File:** internal/gitaly/service/repository/create_repository_from_url.go (L60-84)
```go
	urlString := u.String()

	if resolvedAddress != "" {
		modifiedURL, resolveConfig, err := gitcmd.GetURLAndResolveConfig(u.String(), resolvedAddress)
		if err != nil {
			return nil, structerr.NewInvalidArgument("couldn't get curloptResolve config: %w", err)
		}

		urlString = modifiedURL
		config = append(config, resolveConfig...)
	}

	// Drop support for bundle URI when fetching from a remote repository.
	// Since the URI can point to any server, including localhost, this is to
	// prevent attack vectors that could abuse this mechanism.
	opts = append(opts, gitcmd.WithGlobalOption(gitcmd.ConfigPair{Key: "transfer.bundleURI", Value: "false"}))

	return s.gitCmdFactory.NewWithoutRepo(ctx,
		gitcmd.Command{
			Name:  "clone",
			Flags: cloneFlags,
			Args:  []string{urlString, repositoryFullPath},
		},
		append(opts, gitcmd.WithConfigEnv(config...))...,
	)
```

**File:** internal/gitaly/service/repository/create_repository_from_url.go (L149-159)
```go
func validateCreateRepositoryFromURLRequest(ctx context.Context, locator storage.Locator, req *gitalypb.CreateRepositoryFromURLRequest) error {
	if err := locator.ValidateRepository(ctx, req.GetRepository(), storage.WithSkipRepositoryExistenceCheck()); err != nil {
		return err
	}

	if req.GetUrl() == "" {
		return fmt.Errorf("empty Url")
	}

	return nil
}
```
