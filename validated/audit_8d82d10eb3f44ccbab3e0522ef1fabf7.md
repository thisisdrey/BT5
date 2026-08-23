### Title
Missing overall `http.Client` timeout in `CreateRepositoryFromSnapshot` allows slow-body DoS via crafted `HttpUrl` - ([File: internal/gitaly/service/repository/create_repository_from_snapshot.go])

### Summary
The `CreateRepositoryFromSnapshot` RPC lets a caller supply an arbitrary `HttpUrl` (plus optional `HttpAuth`/`ResolvedAddress`) that Gitaly fetches with a package-level `http.Client` in order to `tar`-extract the response body directly into a newly created repository. That client's `Transport` bounds dial, TLS-handshake, idle-connection and response-*header* time, but the `http.Client` itself never sets an overall `Timeout`, so nothing bounds the time it takes to read the response *body*.

### Finding Description
`httpClient` is constructed once for the whole process: [1](#0-0) 

`httpTransport` sets `DialContext.Timeout`, `IdleConnTimeout`, `TLSHandshakeTimeout`, `ExpectContinueTimeout`, and `ResponseHeaderTimeout`, but the `http.Client{}` literal has no `Timeout` field set. Per Go's `net/http` semantics, `Transport.ResponseHeaderTimeout` only bounds the wait for response *headers*; it does not bound the time spent streaming the response *body*. `http.Client.Timeout` is the only setting that bounds the full request/response round trip including body transfer, and it is absent here.

The vulnerable client is used in `untar`, which streams the HTTP response body straight into a spawned `tar` process as stdin: [2](#0-1) 

`in.GetHttpUrl()` and `in.GetHttpAuth()` are taken directly from the `CreateRepositoryFromSnapshotRequest` (a crafted RPC field), and `newResolvedHTTPClient` even lets the caller pin the DNS-resolved address: [3](#0-2) 

If the caller supplies a URL pointing at a server they control (or that they can otherwise induce Gitaly to reach), that server can respond with headers promptly (satisfying `ResponseHeaderTimeout`) and then trickle the body extremely slowly (a "slow-drip"/"slowloris"-style response) for as long as the underlying gRPC context allows — which, absent an explicit deadline set by the caller, can be indefinite. This holds the goroutine executing `CreateRepositoryFromSnapshot`, the TCP connection, the `tar` subprocess, and the repository-creation lock/transaction open the entire time.

The code comment nearby already acknowledges the trust boundary is weak ("the received archive is trusted a lot... before pointing this RPC at endpoints not under our control, it should undergo a lot of hardening"), but this specific gap — no `http.Client.Timeout` — is a concrete, fixable instance of exactly the "lack of timeout → resource exhaustion" bug class from the referenced report.

### Impact Explanation
Repeated invocations of `CreateRepositoryFromSnapshot` against attacker-controlled or slow-responding URLs can accumulate long-lived goroutines, open file descriptors/sockets, and blocked `tar` subprocesses on the Gitaly node, exhausting resources and degrading or denying service for legitimate RPCs on that node — consistent with the "DoS of a handler" acceptance criterion.

### Likelihood Explanation
Exploitability depends only on the ability to invoke `CreateRepositoryFromSnapshot` with an attacker-influenced `HttpUrl` (e.g., during repository import/migration workflows) and does not require any privileged Gitaly access beyond what's needed to call this RPC; no MITM, leaked token, or malicious-peer assumption is required — a single crafted RPC field suffices.

### Recommendation
Set an explicit overall `Timeout` on the package-level `httpClient` (and on the client returned by `newResolvedHTTPClient`), e.g. bounding the whole request (including body read) to a fixed duration, and/or wrap `rsp.Body` with `io.LimitReader` and a per-read deadline so a slow body cannot stall the `tar` extraction indefinitely. Additionally, consider deriving a bounded `context.WithTimeout` for the `untar` HTTP request independent of the caller-supplied gRPC deadline.

### Proof of Concept
1. Stand up an HTTP server that responds with `200 OK` and headers immediately, then writes the tar body one byte every few seconds (never closing the connection).
2. Call `CreateRepositoryFromSnapshot` with `HttpUrl` pointing at that server and no (or a very long) client-side deadline.
3. Observe the RPC handler, the underlying TCP connection, and the spawned `tar` process remain alive far longer than any of the configured `httpTransport` timeouts would suggest, since none of them bound body-read duration — repeating this concurrently exhausts goroutines/FDs on the Gitaly node.

### Citations

**File:** internal/gitaly/service/repository/create_repository_from_snapshot.go (L25-46)
```go
var httpTransport = &http.Transport{
	Proxy: http.ProxyFromEnvironment,
	DialContext: (&net.Dialer{
		Timeout:   30 * time.Second,
		KeepAlive: 10 * time.Second,
	}).DialContext,
	MaxIdleConns:          2,
	IdleConnTimeout:       30 * time.Second,
	TLSHandshakeTimeout:   10 * time.Second,
	ExpectContinueTimeout: 10 * time.Second,
	ResponseHeaderTimeout: 30 * time.Second,
}

// httpClient defines a http.Client that uses the specialized httpTransport
// (above). It also disables following redirects, as we don't expect this to be
// required for this RPC.
var httpClient = &http.Client{
	Transport: correlation.NewInstrumentedRoundTripper(otelhttp.NewTransport(httpTransport)),
	CheckRedirect: func(*http.Request, []*http.Request) error {
		return http.ErrUseLastResponse
	},
}
```

**File:** internal/gitaly/service/repository/create_repository_from_snapshot.go (L48-85)
```go
// newResolvedHTTPClient is a modified version of the httpClient variable but here we resolve the
// URL to predefined IP:PORT. This is to avoid DNS rebinding.
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
