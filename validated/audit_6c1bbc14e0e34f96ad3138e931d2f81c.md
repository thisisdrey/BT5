## Finding: CRLF Injection via Unvalidated Host in `httputil.DumpRequest`

### Title
CRLF/Header Injection via Unvalidated `Request.Host` in `net/http/httputil.DumpRequest` - (File: src/net/http/httputil/dump.go)

### Summary
`httputil.DumpRequest` serializes an `*http.Request` into a raw HTTP/1.x wire message and writes the `Host` header directly from `req.Host` (or `req.URL.Host`) with `fmt.Fprintf(&b, "Host: %s\r\n", host)`, without any character validation. Unlike `http.Request.Write`, which validates the host via `httpguts.ValidHostHeader` and zeroes it out on failure, `DumpRequest` performs no such check, so a `Host` value containing `\r\n` is copied verbatim into the serialized message, producing injected header lines — the same primitive described in GHSA-hq7v-mx3g-29hw for `guzzlehttp/psr7`.

### Finding Description
An attacker who controls a URL/host string that ends up in `Request.Host` (or `Request.URL.Host`) — e.g. via a proxy, crawler, webhook-forwarder, or debug/logging tool that builds an `*http.Request` from untrusted input and calls `httputil.DumpRequest` to log or forward the raw message — can inject `\r\n` sequences into the host.

Entry point: `httputil.DumpRequest(req *http.Request, body bool)` at [1](#0-0) .

The vulnerable sink: [2](#0-1) 

There is no call to `httpguts.ValidHostHeader` or any character filtering here, unlike the sanctioned code path in `(*http.Request).write`, which explicitly validates and clears an invalid host to prevent smuggling: [3](#0-2) 

Because `DumpRequest` accepts an arbitrary, caller-constructed `*http.Request` (not necessarily one that passed through `http.ReadRequest`/`conn.readRequest`, which do validate the `Host` header via `httpguts.ValidHostHeader`), it is possible to feed it a `Request{Host: "foo.com\r\nX-Injected: yes"}` and get a serialized message with an injected header line.

### Impact Explanation
The output of `DumpRequest` is documented for debugging, but is also commonly forwarded/logged/relayed by proxies, request mirrors, and API gateways. A malformed host containing CRLF injects arbitrary header lines into the dumped/forwarded raw HTTP message, which can contribute to request smuggling, log forging, or header injection into any downstream consumer that trusts the dumped bytes as a well-formed HTTP message — directly analogous to the psr7 advisory's impact (CWE-113/CWE-93, `C:N/I:L`).

### Likelihood Explanation
Any code path that builds an `http.Request` from untrusted input (e.g., `req.Host = untrustedHost` or `req.URL.Host = untrustedHost` without going through `http.ReadRequest`) and then calls `httputil.DumpRequest` to log, mirror, or relay the request is affected. This is a plausible pattern for debugging middlewares, traffic mirrors/tee proxies, and webhook-forwarding tools.

### Recommendation
Have `DumpRequest` validate/sanitize the host the same way `(*http.Request).write` does — call `httpguts.ValidHostHeader` (after `httpguts.PunycodeHostPort`) before writing the `Host:` line, and either reject the request or blank the host on failure, mirroring the logic in `src/net/http/request.go` lines 622-649.

### Proof of Concept
```go
package httputil_test

import (
	"net/http/httputil"
	"net/url"
	"strings"
	"testing"

	"net/http"
)

func TestDumpRequestHostCRLFInjection(t *testing.T) {
	req := &http.Request{
		Method: "GET",
		URL:    &url.URL{Path: "/foo"},
		Host:   "foo.com\r\nX-Injected: yes",
		Header: http.Header{},
	}

	dump, err := httputil.DumpRequest(req, false)
	if err != nil {
		t.Fatalf("DumpRequest error: %v", err)
	}

	out := string(dump)
	if strings.Contains(out, "X-Injected: yes") {
		t.Fatalf("CRLF injection succeeded, dumped request contains injected header:\n%s", out)
	}
}
```
Expected (buggy) behavior: the test fails because `out` contains a `Host: foo.com` line immediately followed by an attacker-controlled `X-Injected: yes` header line, demonstrating that `DumpRequest` does not sanitize the host before serialization.

### Citations

**File:** src/net/http/httputil/dump.go (L217-217)
```go
func DumpRequest(req *http.Request, body bool) ([]byte, error) {
```

**File:** src/net/http/httputil/dump.go (L246-253)
```go
	if !absRequestURI {
		host := req.Host
		if host == "" && req.URL != nil {
			host = req.URL.Host
		}
		if host != "" {
			fmt.Fprintf(&b, "Host: %s\r\n", host)
		}
```

**File:** src/net/http/request.go (L622-649)
```go
	host, err = httpguts.PunycodeHostPort(host)
	if err != nil {
		return err
	}
	// Validate that the Host header is a valid header in general,
	// but don't validate the host itself. This is sufficient to avoid
	// header or request smuggling via the Host field.
	// The server can (and will, if it's a net/http server) reject
	// the request if it doesn't consider the host valid.
	if !httpguts.ValidHostHeader(host) {
		// Historically, we would truncate the Host header after '/' or ' '.
		// Some users have relied on this truncation to convert a network
		// address such as Unix domain socket path into a valid, ignored
		// Host header (see https://go.dev/issue/61431).
		//
		// We don't preserve the truncation, because sending an altered
		// header field opens a smuggling vector. Instead, zero out the
		// Host header entirely if it isn't valid. (An empty Host is valid;
		// see RFC 9112 Section 3.2.)
		//
		// Return an error if we're sending to a proxy, since the proxy
		// probably can't do anything useful with an empty Host header.
		if !usingProxy {
			host = ""
		} else {
			return errors.New("http: invalid Host header")
		}
	}
```
