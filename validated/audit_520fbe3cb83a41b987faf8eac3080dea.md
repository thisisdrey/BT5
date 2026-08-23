No `GIT_ALLOW_PROTOCOL` restriction exists in Gitaly to limit which schemes reach `git clone`. Confirming the proto field docs for `ResolvedAddress` to check documented intent.### Title
DNS-rebinding protection for `git://` URLs is ineffective because `http.curloptResolve` is not honored by the git-daemon transport - (File: internal/git/gitcmd/command_resolve.go)

### Summary
`cloneFromURLCommand` calls `gitcmd.GetURLAndResolveConfig` to pin the resolved IP address for a given `Url`, in order to prevent DNS-rebinding SSRF between the time GitLab Rails validates the destination address and the time Gitaly actually connects. For `git://` URLs, this function only sets `http.curloptResolve`, a libcurl-only option that is exclusively consumed by git's `remote-curl` HTTP(S) transport. The `git://` protocol is implemented by git's native TCP transport (`connect.c`) and never reads `http.*` configuration, so the pinning has zero effect and `git clone` performs a fresh, unconstrained DNS lookup of the attacker-controlled hostname at connection time.

### Finding Description
In `cloneFromURLCommand` (internal/gitaly/service/repository/create_repository_from_url.go:62-70), when `resolvedAddress` is non-empty, the code calls: [1](#0-0) 
`gitcmd.GetURLAndResolveConfig` groups `git://` together with `http://`/`https://` and routes it to `getURLAndResolveConfigForURL`: [2](#0-1) 
That helper leaves the URL string completely unchanged and only emits an `http.curloptResolve` config pair: [3](#0-2) 
`http.curloptResolve` maps to curl's `CURLOPT_RESOLVE`, which is only interpreted by git's `remote-curl` helper (used for `http://`/`https://` remotes). The `git://` protocol is handled entirely inside git's native transport code (`connect.c`), which performs its own `getaddrinfo`/socket connection and never reads `http.*` git config at all. Consequently, for a `git://` URL, the “resolve config” is silently a no-op: the resulting `git clone git://<hostname>/... -c http.curloptResolve=...` command still resolves `<hostname>` via ordinary DNS at execution time, ignoring `resolvedAddress` entirely.

This defeats the entire purpose of the `ResolvedAddress` field, which exists precisely to prevent an attacker from using DNS rebinding: GitLab Rails resolves the hostname, validates the IP is not on an SSRF blocklist (e.g., not a private/internal address), and passes that validated IP to Gitaly via `ResolvedAddress` expecting Gitaly to force the connection to exactly that IP. Because Gitaly's `git://` handling doesn't actually enforce this pinning, an attacker who fully controls both `Url` (`git://attacker-domain/...`) and `ResolvedAddress` can register a domain whose DNS record is swapped between the Rails-side validation and the Gitaly-side clone (classic TOCTOU DNS rebind), causing Gitaly's `git-daemon`-transport clone to connect to an internal/private address of the attacker's choosing, resulting in SSRF against internal services/network via the `CreateRepositoryFromURL` RPC.

No other check in the call path stops this: `validateCreateRepositoryFromURLRequest` only checks that `Url` is non-empty, and does not restrict scheme. `gitcmd.GetURLAndResolveConfig`'s allowlist explicitly includes `git://` under the same (ineffective for this scheme) handling as HTTP(S), giving a false sense of protection.

### Impact Explanation
This is an SSRF class vulnerability: an unprivileged user who can trigger `CreateRepositoryFromURL` (e.g., via project import-by-URL) with an attacker-controlled `Url`/`ResolvedAddress` pair can cause the Gitaly server to make outbound TCP connections (via git's raw git-daemon protocol) to arbitrary internal hosts/ports that were supposed to be blocked by the DNS-rebinding/IP-pinning mitigation. Depending on internal network topology, this can be used to reach internal services (e.g., metadata endpoints, internal git servers, other internal TCP services listening on arbitrary ports since `git://` connects to an attacker-chosen port), and repository content fetched from such internal hosts is imported into the user's own repository, potentially disclosing internal data.

### Likelihood Explanation
The attacker only needs unprivileged capability to trigger a URL-based import/mirror/clone flow that reaches `CreateRepositoryFromURLRequest` with attacker-supplied `Url` and `ResolvedAddress`, as already stated as a precondition in the question. The attack additionally requires the ability to perform DNS rebinding on a domain they control (a well-understood and practical technique), and requires that `git://` URLs are not filtered out earlier by GitLab Rails' URL-scheme allowlist before reaching Gitaly. Within Gitaly itself, there is no scheme restriction, so if such a request reaches Gitaly, the bypass is deterministic and repeatable.

### Recommendation
Do not include `git://` in the same code path as `http://`/`https://` in `GetURLAndResolveConfig`. Either:
- Reject `git://` URLs entirely when `resolvedAddress` is supplied (since there's no reliable git-native mechanism to pin the destination for this protocol), or
- Rewrite the URL to substitute the resolved IP address directly in the host component (similar to the SSH/SCP handling) instead of relying on `http.curloptResolve`, ensuring the actual TCP connection target is the validated IP.

### Proof of Concept
```go
func TestGetURLAndResolveConfig_GitProtocolIsNotPinned(t *testing.T) {
	url, config, err := gitcmd.GetURLAndResolveConfig("git://attacker.example.com/foo/bar", "192.168.0.1")
	require.NoError(t, err)
	// URL is unchanged -- hostname still resolved by git's native transport, not pinned to 192.168.0.1
	require.Equal(t, "git://attacker.example.com/foo/bar", url)
	// Only an HTTP-curl config is emitted; git:// (native transport) never reads http.* config,
	// so this provides no actual protection against DNS rebinding.
	require.Equal(t, []gitcmd.ConfigPair{
		{Key: "http.curloptResolve", Value: "attacker.example.com:9418:192.168.0.1"},
	}, config)
}
```
RPC-level exploitation: send `CreateRepositoryFromURLRequest{Url: "git://attacker.example.com/x.git", ResolvedAddress: "<benign-ip-passing-rails-check>"}`. Have `attacker.example.com`'s DNS record point to the benign IP at request-validation time and to an internal target (e.g. `127.0.0.1` or an internal service IP/port) by the time Gitaly executes `git clone git://attacker.example.com/x.git ...`. The resulting clone connects to the internal target, not the pinned `ResolvedAddress`, confirming the bypass.

### Citations

**File:** internal/gitaly/service/repository/create_repository_from_url.go (L62-70)
```go
	if resolvedAddress != "" {
		modifiedURL, resolveConfig, err := gitcmd.GetURLAndResolveConfig(u.String(), resolvedAddress)
		if err != nil {
			return nil, structerr.NewInvalidArgument("couldn't get curloptResolve config: %w", err)
		}

		urlString = modifiedURL
		config = append(config, resolveConfig...)
	}
```

**File:** internal/git/gitcmd/command_resolve.go (L41-49)
```go
	switch {
	case strings.HasPrefix(remoteURL, "http://"), strings.HasPrefix(remoteURL, "https://"), strings.HasPrefix(remoteURL, "git://"):
		return getURLAndResolveConfigForURL(remoteURL, resolvedAddress)
	case strings.HasPrefix(remoteURL, "ssh://"):
		return getURLAndResolveConfigForSSH(remoteURL, resolvedAddress)
	default:
		return getURLAndResolveConfigForSCP(remoteURL, resolvedAddress)
	}
}
```

**File:** internal/git/gitcmd/command_resolve.go (L81-105)
```go
func getURLAndResolveConfigForURL(remoteURL, resolvedAddress string) (string, []ConfigPair, error) {
	u, err := url.ParseRequestURI(remoteURL)
	if err != nil {
		return "", nil, fmt.Errorf("couldn't parse remoteURL: %w", err)
	}

	port := u.Port()

	if port == "" {
		switch u.Scheme {
		case "http":
			port = "80"
		case "https":
			port = "443"
		case "git":
			port = "9418"
		default:
			return "", nil, fmt.Errorf("unknown schema provided: %s", u.Scheme)
		}
	}

	return remoteURL, []ConfigPair{
		{Key: "http.curloptResolve", Value: fmt.Sprintf("%s:%s:%s", u.Hostname(), port, resolvedAddress)},
	}, nil
}
```
