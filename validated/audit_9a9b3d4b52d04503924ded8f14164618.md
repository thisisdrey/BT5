### Title
Arbitrary Command Execution via Unrestricted Git Transport Scheme in `CreateRepositoryFromURL` (`ext::` Helper) - ([File: internal/gitaly/service/repository/create_repository_from_url.go])

### Summary
`CreateRepositoryFromURL` takes a caller-supplied `url` field and passes it, largely unmodified, straight into a `git clone` subprocess argument. Gitaly does not restrict which Git transport *scheme* the URL may use, nor does it set `GIT_ALLOW_PROTOCOL`/`protocol.*.allow` to disable dangerous "remote helper" transports such as `ext::`. Git's `ext::` transport executes an arbitrary shell command as specified in the URL itself. This mirrors the reported bug class: a privileged operation (`compound()`'s `delegatecall`) whose *target/behavior* is fully controlled by dynamic, externally supplied input, allowing the caller to redirect execution into attacker-chosen code instead of the intended git-remote target.

### Finding Description
`cloneFromURLCommand` parses the request URL, strips any embedded credentials into an `Authorization` header, and then builds the `git clone` invocation with the (still attacker-controlled) URL string as a positional argument: [1](#0-0) 

The only validation performed on the request is that the URL is non-empty: [2](#0-1) 

Gitaly's generic command-argument sanitizer (`gitcmd.Command.CommandArgs`) only prevents positional arguments from beginning with `-` (to stop flag/argument injection); it performs no scheme or protocol allow-listing: [3](#0-2) 

Because the URL does not start with `-`, it passes this check unchanged. Git itself supports the `ext::<command>` transport helper (`git help remote-helpers`), which spawns `<command>` via the shell to perform the "clone." A crafted URL such as `ext::sh -c "id > /tmp/pwned"` or (for exfiltration/SSRF/RCE) any shell pipeline is a syntactically valid Git remote URL that Gitaly will hand directly to `git-clone(1)`. No code path in the repository sets `GIT_ALLOW_PROTOCOL`, `protocol.ext.allow`, or an equivalent scheme allow-list — a targeted search across the codebase for these mitigations returned no hits outside of unrelated DNS-rebinding code in `command_resolve.go`.

This is the direct structural analog of the reported `delegatecall` issue: in both cases, a privileged operation is parameterized by a value that is fully attacker-controlled, and the underlying primitive (`delegatecall` / `ext::` remote helper) treats that value as *executable* rather than as inert data, letting the caller substitute their own code for the intended target.

### Impact Explanation
Any client authorized to invoke `CreateRepositoryFromURL` (used by GitLab's project-import and Geo-replication flows) can achieve arbitrary command execution on the Gitaly node by supplying an `ext::`-scheme URL instead of a normal `http(s)/git/ssh` URL. This is a full remote-code-execution primitive on the Gitaly server process — a strictly worse outcome than the "self-destruct" scenario in the reference report, since it grants the attacker a shell rather than merely destroying a contract.

### Likelihood Explanation
Reachability requires only that the caller can invoke the `CreateRepositoryFromURL` RPC with attacker-influenced content for the `url` field — a normal, unprivileged-facing operation that ordinary GitLab-level "import project by URL" or Geo-mirroring features would route through, without any admin-only gating inside Gitaly itself. No additional git configuration, feature flag, or admin action is needed for the transport to be reachable, since Gitaly never disables `ext::` (or other remote-helper) transports before invoking `git clone`.

### Recommendation
- Disallow non-allow-listed URL schemes before constructing the clone command — restrict to `http://`, `https://`, `git://`, and `ssh://`, rejecting everything else (in particular `ext::`, `fd::`, and any other remote-helper prefix) in `validateCreateRepositoryFromURLRequest` / `cloneFromURLCommand`.
- Defense-in-depth: explicitly set `GIT_ALLOW_PROTOCOL=http:https:git:ssh` (or the equivalent `protocol.*.allow=never` config pairs) on every subprocess spawned for clone/fetch operations that accept caller-supplied URLs (`CreateRepositoryFromURL`, `FetchRemote`, etc.), not just the "no bundle URI" mitigation already applied for SSRF.
- Add regression tests asserting that `ext::`, `fd::`, and other remote-helper URL schemes are rejected by `CreateRepositoryFromURL` and `FetchRemote`.

### Proof of Concept
```
grpc_cli call gitaly.RepositoryService.CreateRepositoryFromURL '
repository { storage_name: "default" relative_path: "imports/pwn.git" }
url: "ext::sh -c \"touch /tmp/gitaly-pwned\""
'
```
Gitaly builds and runs:
```
git clone --quiet --bare -- "ext::sh -c \"touch /tmp/gitaly-pwned\"" <target-path>
```
which invokes Git's `ext::` remote helper, executing `sh -c "touch /tmp/gitaly-pwned"` on the Gitaly host as the Gitaly service user. [4](#0-3)

### Citations

**File:** internal/gitaly/service/repository/create_repository_from_url.go (L36-85)
```go
	u, err := url.Parse(repoURL)
	if err != nil {
		return nil, structerr.NewInternal("%w", err)
	}

	var config []gitcmd.ConfigPair
	if u.User != nil {
		password, hasPassword := u.User.Password()

		var creds string
		if hasPassword {
			creds = u.User.Username() + ":" + password
		} else {
			creds = u.User.Username()
		}

		u.User = nil
		authHeader := fmt.Sprintf("Authorization: Basic %s", base64.StdEncoding.EncodeToString([]byte(creds)))
		config = append(config, gitcmd.ConfigPair{Key: "http.extraHeader", Value: authHeader})
	} else if len(authorizationToken) > 0 {
		authHeader := fmt.Sprintf("Authorization: %s", authorizationToken)
		config = append(config, gitcmd.ConfigPair{Key: "http.extraHeader", Value: authHeader})
	}

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
}
```

**File:** internal/gitaly/service/repository/create_repository_from_url.go (L87-90)
```go
func (s *server) CreateRepositoryFromURL(ctx context.Context, req *gitalypb.CreateRepositoryFromURLRequest) (*gitalypb.CreateRepositoryFromURLResponse, error) {
	if err := validateCreateRepositoryFromURLRequest(ctx, s.locator, req); err != nil {
		return nil, structerr.NewInvalidArgument("%w", err)
	}
```

**File:** internal/git/gitcmd/command_description.go (L446-451)
```go
func validatePositionalArg(arg string) error {
	if strings.HasPrefix(arg, "-") {
		return fmt.Errorf("positional arg %q cannot start with dash '-': %w", arg, ErrInvalidArg)
	}
	return nil
}
```
