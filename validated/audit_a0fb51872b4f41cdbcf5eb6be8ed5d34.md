### Title
SSRF via unrestricted `HttpUrl`/`ResolvedAddress` in `CreateRepositoryFromSnapshot` - ([File: internal/gitaly/service/repository/create_repository_from_snapshot.go])

### Summary
`untar` builds a GET request directly from the attacker-controlled `CreateRepositoryFromSnapshotRequest.HttpUrl` and, if `ResolvedAddress` is supplied, dials that exact address/port instead of resolving DNS. `newResolvedHTTPClient` only checks that `ResolvedAddress` parses as a valid IP, without excluding loopback, link-local, or other private ranges, and does not restrict the URL scheme beyond `http`/`https` port defaulting. This allows requests to be forced to internal/metadata endpoints, with the attacker-supplied `HttpAuth` header attached, and the response is subsequently piped into `tar -xvf -`.

### Finding Description
In `untar` (internal/gitaly/service/repository/create_repository_from_snapshot.go:87-121), the request URL comes straight from `in.GetHttpUrl()` with no scheme allowlist beyond what `newResolvedHTTPClient` enforces when `ResolvedAddress` is set (line 88, 94-99). `newResolvedHTTPClient` (lines 50-85) validates only that `resolvedAddress` is `net.ParseIP`-parseable (line 69) and that the scheme is `http` or `https` for port defaulting (lines 58-66); it does not reject private, loopback, or link-local addresses (e.g. `127.0.0.1`, `169.254.169.254`, RFC1918 ranges). The custom `DialContext` (lines 74-76) forces the TCP connection to `resolvedAddress:port` regardless of what the URL's host is, meaning even if the hostname in `HttpUrl` looks legitimate, the actual destination is whatever IP the caller supplies — this is explicitly there to prevent DNS-rebinding, but it does not prevent SSRF to internal ranges. If `HttpAuth` is set, it's attached to the request as an `Authorization` header (lines 101-103) and would be sent to whatever endpoint is reached. There's no `storage.ValidateRelativePath`-style check applicable here since this is a network destination check, not a path check, and none of the existing SanitizeString/allowlist mechanisms apply to URLs.

The comment at line 140-141 ("the received archive is trusted *a lot* ... before pointing this RPC at endpoints not under our control, it should undergo a lot of hardening") indicates the Gitaly maintainers are aware this RPC's HTTP-fetch behavior is fundamentally unhardened and intended to be used only with trusted, operator-controlled URLs (this is the design assumption for the Geo/replication use case), not attacker-supplied arbitrary internal targets.

### Impact Explanation
If reachable with attacker-controlled `HttpUrl`/`ResolvedAddress`, this enables SSRF: the Gitaly process can be forced to issue authenticated GET requests to internal-only endpoints (e.g., cloud metadata services, internal admin APIs) and exfiltrate the response into the resulting repository content via `tar -xvf -`, which an attacker can then read back by fetching the created repository. This corresponds to GitLab's SSRF impact class, potentially combined with credential disclosure via the injected `Authorization` header.

### Likelihood Explanation
This RPC is intended to be triggered as part of Geo/replication-type internal workflows, not exposed as an ordinary user-facing GitLab Rails API endpoint that lets an arbitrary unprivileged user set `HttpUrl`/`ResolvedAddress` freely. Whether an unprivileged, unauthenticated-to-Gitaly attacker (a regular GitLab user with no special role) can actually control both `HttpUrl` and `ResolvedAddress` end-to-end depends on the GitLab Rails call sites/authorization for this RPC, which are outside this repository and were not found in the indexed code (no callers of `CreateRepositoryFromSnapshot` from Rails were locatable here; only the Gitaly RPC implementation, its Go client stubs, and tests). Without confirming that an ordinary user can reach this RPC with arbitrary values for these two fields (as opposed to it being restricted to Geo secondary nodes replicating from a primary under operator control), the precondition in the question cannot be verified from the available Gitaly codebase alone.

### Recommendation
In `newResolvedHTTPClient`, reject `resolvedAddress` values that are loopback, link-local (including `169.254.0.0/16` and IPv6 link-local), and private/reserved ranges unless explicitly configured otherwise; also validate/restrict the `HttpUrl` scheme to `http`/`https` unconditionally (even when `ResolvedAddress` is empty) and consider requiring Rails to supply and validate `ResolvedAddress` against an SSRF allowlist before invoking this RPC. Enforcing this at the Gitaly layer is a defense-in-depth improvement even if primary enforcement is expected in GitLab Rails.

### Proof of Concept
Not confirmable as a full RPC-level PoC without knowing the GitLab Rails authorization path that allows an unprivileged user to invoke `CreateRepositoryFromSnapshot` with attacker-chosen `HttpUrl`/`ResolvedAddress`. At the Gitaly unit level, the following demonstrates the missing address-range validation:

```go
func TestNewResolvedHTTPClient_AllowsLinkLocal(t *testing.T) {
    client, err := newResolvedHTTPClient("http://example.com/", "169.254.169.254")
    require.NoError(t, err) // currently succeeds; should be rejected
    _ = client
}
``` [1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** internal/gitaly/service/repository/create_repository_from_snapshot.go (L50-85)
```go
func newResolvedHTTPClient(httpAddress, resolvedAddress string) (*http.Client, error) {
	url, err := url.ParseRequestURI(httpAddress)
	if err != nil {
		return nil, structerr.NewInvalidArgument("parsing HTTP URL: %w", err)
	}

	port := url.Port()
	if port == "" {
		switch url.Scheme {
		case "http":
			port = "80"
		case "https":
			port = "443"
		default:
			return nil, structerr.NewInvalidArgument("unsupported schema %q", url.Scheme)
		}
	}

	// Sanity-check whether the resolved address is a valid IP address.
	if net.ParseIP(resolvedAddress) == nil {
		return nil, structerr.NewInvalidArgument("invalid resolved address %q", resolvedAddress)
	}

	transport := httpTransport.Clone()
	transport.DialContext = func(ctx context.Context, network, _ string) (net.Conn, error) {
		return httpTransport.DialContext(ctx, network, fmt.Sprintf("%s:%s", resolvedAddress, port))
	}

	return &http.Client{
		Transport: correlation.NewInstrumentedRoundTripper(otelhttp.NewTransport(transport)),
		// Here we directly return the `ErrUseLastResponse` to prevent redirects
		CheckRedirect: func(*http.Request, []*http.Request) error {
			return http.ErrUseLastResponse
		},
	}, nil
}
```

**File:** internal/gitaly/service/repository/create_repository_from_snapshot.go (L87-121)
```go
func (s *server) untar(ctx context.Context, path string, in *gitalypb.CreateRepositoryFromSnapshotRequest) error {
	req, err := http.NewRequestWithContext(ctx, "GET", in.GetHttpUrl(), nil)
	if err != nil {
		return structerr.NewInvalidArgument("Bad HTTP URL: %w", err)
	}

	client := httpClient
	if resolvedAddress := in.GetResolvedAddress(); resolvedAddress != "" {
		client, err = newResolvedHTTPClient(in.GetHttpUrl(), resolvedAddress)
		if err != nil {
			return structerr.NewInvalidArgument("creating resolved HTTP client: %w", err)
		}
	}

	if in.GetHttpAuth() != "" {
		req.Header.Set("Authorization", in.GetHttpAuth())
	}

	rsp, err := client.Do(req)
	if err != nil {
		return structerr.NewInternal("HTTP request failed: %w", err)
	}
	defer rsp.Body.Close()

	if rsp.StatusCode < http.StatusOK || rsp.StatusCode >= http.StatusMultipleChoices {
		return structerr.NewInternal("HTTP server: %s", rsp.Status)
	}

	cmd, err := command.New(ctx, s.logger, []string{"tar", "-C", path, "-xvf", "-"}, command.WithStdin(rsp.Body))
	if err != nil {
		return err
	}

	return cmd.Wait()
}
```

**File:** internal/gitaly/service/repository/create_repository_from_snapshot.go (L140-141)
```go
		// NOTE: The received archive is trusted *a lot*. Before pointing this RPC
		// at endpoints not under our control, it should undergo a lot of hardening.
```
