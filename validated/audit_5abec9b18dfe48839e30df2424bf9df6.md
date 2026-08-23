Given the reachable pattern in this repo, here's the analog finding based on concrete code:

### Title
DNS-rebinding SSRF protection bypass via URL host-parser differential between Go's `net/url` and Git/libcurl - (File: `internal/git/gitcmd/command_resolve.go`)

### Summary
Gitaly implements a `resolved_address` pinning mechanism intended to close the classic "check URL, then fetch URL" SSRF/DNS-rebinding gap: an upstream caller (e.g. GitLab Rails, for repository import or pull-mirror configuration) resolves and validates a remote URL's hostname, then passes both the original URL and the validated IP to Gitaly so the actual `git` request is forced to hit that specific IP rather than re-resolving DNS. This pinning is implemented purely with Go's standard-library URL parser to extract the "host" component, while the actual network request is executed by `git`/libcurl using its own, historically more lenient URL parser — the exact parser-differential bug class behind CVE-2022-27780.

### Finding Description
For HTTP/HTTPS/git:// remotes, `getURLAndResolveConfigForURL` derives the pinned host purely from Go's `net/url.ParseRequestURI`: [1](#0-0) 

Critically, the *original, unmodified* `remoteURL` string is what actually gets sent to `git` (and ultimately libcurl) — only a `http.curloptResolve=HOST:PORT:IP` config pair is added: [2](#0-1) [3](#0-2) 

`http.curloptResolve` only takes effect when the host:port that curl *itself* resolves out of the URL matches exactly the `HOST:PORT` component supplied in the config. Because the "host" string used to build that config comes from Go's RFC3986-strict parser rather than from curl's own (documented-lenient) URL parsing logic, any URL construction where the two parsers disagree on host boundaries (e.g. unusual userinfo/`@` placement, embedded control or percent-encoded characters, or other URL-confusion constructs of the class documented in CVE-2022-27780) causes:
1. Go's parser to extract host `A` (used to build `http.curloptResolve=A:port:IP`), while
2. curl connects to a different host `B` extracted from the same raw string.

Since `B != A`, curl's `CURLOPT_RESOLVE` mapping silently does not apply, and curl falls back to ordinary DNS resolution for `B` — completely defeating the purpose of `resolved_address`, which the protobuf comments explicitly describe as being "used to avoid DNS rebinding by mapping the url to the resolved address": [4](#0-3) 

This same in-repo comment pattern appears for every RPC exposing `resolved_address` (`FetchRemoteRequest`/`Remote`, `UpdateRemoteMirrorRequest.Remote`, `FindRemoteRootRefRequest`), all funneling into the same `GetURLAndResolveConfig` helper: [5](#0-4) [6](#0-5) [7](#0-6) 

### Impact Explanation
If Gitaly's Go-side host extraction and curl's actual host resolution diverge for a crafted URL, the SSRF/DNS-rebinding mitigation is bypassed entirely, letting `git-clone(1)`/`git-fetch(1)` connect to an attacker-chosen internal address (metadata services, internal admin endpoints, etc.) even though the upstream allow-listing + resolved-address-pinning flow was specifically designed to prevent exactly this. This is reachable from ordinary user-triggered flows: repository import by URL (`CreateRepositoryFromURL`), pull mirroring (`FetchRemote`), push mirroring (`UpdateRemoteMirror`), and `FindRemoteRootRef`, all of which accept attacker-influenced `url`/`remote_url` fields directly in the RPC request.

### Likelihood Explanation
Exploitability is conditional on real curl/`net/url` parsing divergences existing for the crafted input given the currently vendored git/curl version, so this is not proven without version-specific fuzzing/testing of the parser pair — but the code path itself provides no independent, Gitaly-side revalidation that the *actual* URL/host git will use matches the one used to build the pinning, and the changelog shows this general SSRF-in-URL-cloning surface is an active area of concern already being hardened (e.g., disabling `transfer.bundleURI` specifically to close another SSRF vector in this exact code path): [8](#0-7) [9](#0-8) 

### Recommendation
Do not rely on Go's `net/url` to derive the host used for `http.curloptResolve` pinning. Instead, either (a) rewrite the URL to directly embed the resolved IP as the connection target (as is already done for SSH/SCP-style URLs) rather than relying on curl's opt-in resolve mapping, or (b) perform a canonicalization/consistency check that fails closed if Gitaly cannot conclusively determine that the parser used to build the pinning config and curl's own parser will agree on the same host/port for the given raw URL string.

### Proof of Concept
Not independently verified against the vendored curl version in this repo; would require constructing a `CreateRepositoryFromURLRequest.url` (or `FetchRemoteRequest.remote_params.url`) whose raw string parses to a different `Host` under `net/url.ParseRequestURI` than under curl's URL parser, then confirming via `GIT_CONFIG_KEY_*`/`GIT_CONFIG_VALUE_*` env inspection (as done in the existing test at [10](#0-9) ) that the emitted `http.curloptResolve` host does not match the host curl actually contacts.

### Citations

**File:** internal/git/gitcmd/command_resolve.go (L27-49)
```go
func GetURLAndResolveConfig(remoteURL string, resolvedAddress string) (string, []ConfigPair, error) {
	if remoteURL == "" {
		return "", nil, fmt.Errorf("URL is empty")
	}

	if resolvedAddress == "" {
		return "", nil, fmt.Errorf("resolved address is empty")
	}

	resolvedIP := net.ParseIP(resolvedAddress)
	if resolvedIP == nil {
		return "", nil, fmt.Errorf("resolved address has invalid IPv4/IPv6 address")
	}

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

**File:** internal/git/gitcmd/command_resolve.go (L81-104)
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
```

**File:** internal/gitaly/service/repository/create_repository_from_url.go (L60-70)
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
```

**File:** internal/gitaly/service/repository/create_repository_from_url.go (L72-75)
```go
	// Drop support for bundle URI when fetching from a remote repository.
	// Since the URI can point to any server, including localhost, this is to
	// prevent attack vectors that could abuse this mechanism.
	opts = append(opts, gitcmd.WithGlobalOption(gitcmd.ConfigPair{Key: "transfer.bundleURI", Value: "false"}))
```

**File:** internal/gitaly/service/repository/fetch_remote.go (L270-280)
```go
	if resolvedAddress := req.GetRemoteParams().GetResolvedAddress(); resolvedAddress != "" {
		modifiedURL, resolveConfig, err := gitcmd.GetURLAndResolveConfig(remoteURL, resolvedAddress)
		if err != nil {
			return fmt.Errorf("couldn't get curloptResolve config: %w", err)
		}

		remoteURL = modifiedURL
		config = append(config, resolveConfig...)
	}

	config = append(config, gitcmd.ConfigPair{Key: "remote.inmemory.url", Value: remoteURL})
```

**File:** proto/repository.proto (L878-884)
```text
  // resolved_address holds the resolved IP address of the remote_url. This is
  // used to avoid DNS rebinding by mapping the url to the resolved address.
  // Only IPv4 dotted decimal ("192.0.2.1"), IPv6 ("2001:db8::68"), or IPv4-mapped
  // IPv6 ("::ffff:192.0.2.1") forms are supported.
  // Works with HTTP/HTTPS/Git/SSH protocols.
  // Optional.
  string resolved_address = 6;
```

**File:** internal/gitaly/service/remote/update_remote_mirror.go (L76-91)
```go
	var remoteConfig []gitcmd.ConfigPair
	remoteURL := remote.GetUrl()

	if resolvedAddress := remote.GetResolvedAddress(); resolvedAddress != "" {
		modifiedURL, resolveConfig, err := gitcmd.GetURLAndResolveConfig(remoteURL, resolvedAddress)
		if err != nil {
			return fmt.Errorf("couldn't get curloptResolve config: %w", err)
		}

		remoteURL = modifiedURL
		remoteConfig = append(remoteConfig, resolveConfig...)
	}

	remoteConfig = append(remoteConfig, gitcmd.ConfigPair{
		Key: fmt.Sprintf("remote.%s.url", remoteName), Value: remoteURL,
	})
```

**File:** internal/gitaly/service/remote/find_remote_root_ref.go (L17-29)
```go
func (s *server) findRemoteRootRefCmd(ctx context.Context, request *gitalypb.FindRemoteRootRefRequest) (*command.Command, error) {
	remoteURL := request.GetRemoteUrl()
	var config []gitcmd.ConfigPair

	if resolvedAddress := request.GetResolvedAddress(); resolvedAddress != "" {
		modifiedURL, resolveConfig, err := gitcmd.GetURLAndResolveConfig(remoteURL, resolvedAddress)
		if err != nil {
			return nil, structerr.NewInvalidArgument("couldn't get curloptResolve config: %w", err)
		}

		remoteURL = modifiedURL
		config = append(config, resolveConfig...)
	}
```

**File:** CHANGELOG.md (L96-98)
```markdown
### Security (1 change)

- [repoFromURL: Disable bundle URIs when cloning from URL to prevent SSRF](gitlab-org/gitaly@c5ce7f5a396b2e8a942be5bc591dafa20b4395f4)
```

**File:** internal/gitaly/service/remote/find_remote_root_ref_test.go (L180-193)
```go
		{
			desc: "resolved address is present",
			request: &gitalypb.FindRemoteRootRefRequest{
				RemoteUrl:       originalURL,
				ResolvedAddress: "127.0.0.1",
				Repository:      repo,
			},
			expectedConfig: []string{
				"GIT_CONFIG_KEY_0=http.curloptResolve",
				fmt.Sprintf("GIT_CONFIG_VALUE_0=example.com:%d:127.0.0.1", port),
				"GIT_CONFIG_KEY_1=remote.inmemory.url",
				"GIT_CONFIG_VALUE_1=" + originalURL,
			},
		},
```
