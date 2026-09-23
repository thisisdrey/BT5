## Analysis Result

### Title
Host Header Injection via `@` Delimiter Confusion in `net/http.stripHostPort` Enables ServeMux Host-Pattern Routing Bypass - (File: `src/net/http/server.go`)

### Summary
`net/http`'s `ServeMux` supports host-qualified routing patterns (e.g. `"admin.internal.com/"`) that are matched by stripping the port from the raw, attacker-controlled `Host` header via `stripHostPort`, which simply delegates to `net.SplitHostPort`. Because `net.SplitHostPort` splits on the *last* colon and does not validate that the trailing "port" portion is actually numeric or free of `@`/userinfo-like content, an attacker can craft a `Host` header such as `admin.internal.com:garbage@attacker.com` so that the extracted hostname equals a victim host pattern that was never actually intended for this request, causing `ServeMux` to route to the host-restricted handler. This mirrors the exact root cause of the Koa advisory: naive colon-splitting of the raw Host header without RFC 3986 authority validation, allowing an attacker-chosen prefix to be misinterpreted as the "real" hostname.

### Finding Description
Attacker input: an unauthenticated raw `Host` header, e.g. `admin.internal.com:x@attacker.com`, reaching the Go entry point `ServeMux.findHandler` (called from `ServeMux.ServeHTTP`/`ServeMux.Handler`) in [1](#0-0)  where `host = stripHostPort(r.Host)` is computed for non-CONNECT requests. `stripHostPort` is defined as: [2](#0-1) 
It defers entirely to `net.SplitHostPort`, whose splitting logic in [3](#0-2)  finds the **last** `:` and treats everything after it as "port" without validating it is numeric — historically intentional (Go allows symbolic service names like `"golang.org:https%foo"`, per [4](#0-3) ). For input `"admin.internal.com:x@attacker.com"` there is exactly one colon, so `SplitHostPort` succeeds and returns `host="admin.internal.com"`, `port="x@attacker.com"`, with no error — the `@`-containing "port" is accepted verbatim, and `stripHostPort` returns `"admin.internal.com"` unchanged. This value is then fed into the routing tree match at [5](#0-4) , which selects handlers by host-qualified `pattern.host` (documented feature described at [6](#0-5) ). The failed check is the absence of authority/userinfo validation (no rejection of `@` in the host component, unlike `net/url.parseAuthority`, which correctly rejects/splits userinfo via `strings.LastIndex(authority, "@")` in [7](#0-6) ). The sink is the host-based dispatch decision in `ServeMux`, which selects a handler based on the spoofed hostname string rather than the actual destination the request was sent to.

### Impact Explanation
Applications that use `http.ServeMux`'s built-in host-pattern feature to separate handlers by virtual host (e.g., an internal/admin pattern like `"admin.internal.com/"` co-located on the same mux as public patterns, reachable when the server directly terminates connections or a misconfigured/permissive proxy forwards the raw client `Host` header) can have routing decisions manipulated by a crafted `Host` header containing an embedded `@`. This is a routing/authorization-boundary confusion (CWE-20/CWE-290-adjacent), not memory corruption or RCE — impact is limited to handler-selection confusion within the same process, and Go's own documentation already warns that Host header trust for authorization decisions is the handler's responsibility (`server.go:2827` doc comment on `Handler`, and the request.go `Host` field doc noting Handlers must validate). Given this pre-existing documented caveat and the requirement of a specific, less-common application architecture (host-pattern ServeMux used as a security boundary with an untrusted-Host-forwarding front end), this would likely be assessed as **PUBLIC/low-priority** track rather than an unknown high-severity defect — it is a genuine parsing inconsistency in stdlib code, but its exploitability depends heavily on application design choices Go already flags as risky.

### Likelihood Explanation
Requires: (1) a Go HTTP server using `ServeMux`'s host-qualified pattern syntax for meaningfully different access domains, and (2) the raw client `Host` header reaching the server unmodified/unvalidated (no proxy normalizing Host, or `httputil.ReverseProxy`'s default X-Forwarded-Host-preserving behavior noted at [8](#0-7) ). This is a real but narrower victim workflow than Koa's, since Go's ServeMux host-pattern feature is less commonly used as a security boundary than Koa's `ctx.hostname` is used for URL generation.

### Recommendation
Harden `stripHostPort` in `src/net/http/server.go` to reject Host values whose "port" segment (as returned by `net.SplitHostPort`) contains characters outside `[0-9]` (or is otherwise non-numeric/contains `@`), falling back to treating the whole header as an unmatched/invalid host rather than silently accepting a spoofed prefix. This restores RFC 3986 host-component semantics instead of relying on the historically lenient `net.SplitHostPort`.

### Proof of Concept
```go
package http_test

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestServeMuxHostHeaderInjection(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("admin.internal.com/", func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte("admin-panel"))
	})
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte("public"))
	})

	req := httptest.NewRequest("GET", "http://irrelevant/", nil)
	// Attacker-controlled Host header: userinfo-like suffix after '@'
	req.Host = "admin.internal.com:secret@attacker.com"

	_, pattern := mux.Handler(req)
	if pattern != "admin.internal.com/" {
		t.Fatalf("expected spoofed match on admin.internal.com/, got pattern=%q", pattern)
	}
}
```
Expected assertion: `pattern == "admin.internal.com/"` even though the request was never actually addressed to `admin.internal.com`, demonstrating that `stripHostPort` extracts an attacker-chosen hostname prefix from a malformed `Host` header containing `@`, analogous to Koa's `ctx.hostname` colon-splitting flaw.

### Citations

**File:** src/net/http/server.go (L2684-2686)
```go
// A pattern with no host matches every host.
// A pattern with a host matches URLs on that host only.
//
```

**File:** src/net/http/server.go (L2810-2821)
```go
// stripHostPort returns h without any trailing ":<port>".
func stripHostPort(h string) string {
	// If no port on host, return unchanged
	if !strings.Contains(h, ":") {
		return h
	}
	host, _, err := net.SplitHostPort(h)
	if err != nil {
		return h // on error, return unchanged
	}
	return host
}
```

**File:** src/net/http/server.go (L2871-2876)
```go
		n, matches, _ = mux.matchOrRedirect(r.Host, r.Method, path, nil)
	} else {
		// All other requests have any port stripped and path cleaned
		// before passing to mux.handler.
		host = stripHostPort(r.Host)
		path = cleanPath(path)
```

**File:** src/net/http/server.go (L2917-2921)
```go
func (mux *ServeMux) matchOrRedirect(host, method, path string, u *url.URL) (_ *routingNode, matches []string, redirectTo *url.URL) {
	mux.mu.RLock()
	defer mux.mu.RUnlock()

	n, matches := mux.tree.match(host, method, path)
```

**File:** src/net/ipsock.go (L165-218)
```go
func SplitHostPort(hostport string) (host, port string, err error) {
	const (
		missingPort   = "missing port in address"
		tooManyColons = "too many colons in address"
	)
	addrErr := func(addr, why string) (host, port string, err error) {
		return "", "", &AddrError{Err: why, Addr: addr}
	}
	j, k := 0, 0

	// The port starts after the last colon.
	i := bytealg.LastIndexByteString(hostport, ':')
	if i < 0 {
		return addrErr(hostport, missingPort)
	}

	if hostport[0] == '[' {
		// Expect the first ']' just before the last ':'.
		end := bytealg.IndexByteString(hostport, ']')
		if end < 0 {
			return addrErr(hostport, "missing ']' in address")
		}
		switch end + 1 {
		case len(hostport):
			// There can't be a ':' behind the ']' now.
			return addrErr(hostport, missingPort)
		case i:
			// The expected result.
		default:
			// Either ']' isn't followed by a colon, or it is
			// followed by a colon that is not the last one.
			if hostport[end+1] == ':' {
				return addrErr(hostport, tooManyColons)
			}
			return addrErr(hostport, missingPort)
		}
		host = hostport[1:end]
		j, k = 1, end+1 // there can't be a '[' resp. ']' before these positions
	} else {
		host = hostport[:i]
		if bytealg.IndexByteString(host, ':') >= 0 {
			return addrErr(hostport, tooManyColons)
		}
	}
	if bytealg.IndexByteString(hostport[j:], '[') >= 0 {
		return addrErr(hostport, "unexpected '[' in address")
	}
	if bytealg.IndexByteString(hostport[k:], ']') >= 0 {
		return addrErr(hostport, "unexpected ']' in address")
	}

	port = hostport[i+1:]
	return host, port, nil
}
```

**File:** src/net/ip_test.go (L770-772)
```go
		// Opaque service name
		{"golang.org:https%foo", "golang.org", "https%foo"}, // Go 1 behavior
	} {
```

**File:** src/net/url/url.go (L522-528)
```go
func parseAuthority(scheme, authority string) (user *Userinfo, host string, err error) {
	i := strings.LastIndex(authority, "@")
	if i < 0 {
		host, err = parseHost(scheme, authority)
	} else {
		host, err = parseHost(scheme, authority[i+1:])
	}
```

**File:** src/net/http/httputil/reverseproxy.go (L213-216)
```go
	//   - X-Forwarded-For, X-Forwarded-Host, and X-Forwarded-Proto
	//     headers in inbound requests are preserved by default,
	//     which can permit IP spoofing if the Director function is
	//     not careful to remove these headers.
```
