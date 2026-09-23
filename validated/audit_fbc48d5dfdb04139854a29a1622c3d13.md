### Title
Percent-encoded path traversal bypass via raw-path preservation in `httputil.ReverseProxy` path joining - ([File: src/net/http/httputil/reverseproxy.go])

### Summary
`ReverseProxy`'s legacy `Director` path (built via `NewSingleHostReverseProxy`) and `rewriteRequestURL` join the configured target's base path with the inbound request path using `joinURLPath`, which explicitly uses `EscapedPath()`/`RawPath` to preserve the original percent-encoding of the client-supplied path segment rather than the decoded, semantically-normalized path. Just like the Nitro `routeRules` proxy bug, a client can send a percent-encoded traversal sequence (`..%2f`) that is treated as an opaque path segment by Go's own dispatch/normalization logic, but is forwarded byte-for-byte to the upstream, where it can be decoded and resolved outside the intended base scope.

### Finding Description
Attacker input: an HTTP request such as `GET /base/..%2Fadmin/secret` sent to a Go reverse-proxy server built with `httputil.NewSingleHostReverseProxy(target)` where `target.Path` is `/base`.

- Entry point: `ReverseProxy.ServeHTTP` (`src/net/http/httputil/reverseproxy.go:421`) clones the request and, for the deprecated `Director` path, calls `p.Director(outreq)` at `src/net/http/httputil/reverseproxy.go:475-476`, which for `NewSingleHostReverseProxy` invokes `rewriteRequestURL(req, target)` (`src/net/http/httputil/reverseproxy.go:350-355`).
- Sink: `rewriteRequestURL` (`src/net/http/httputil/reverseproxy.go:357-367`) sets `req.URL.Path, req.URL.RawPath = joinURLPath(target, req.URL)`.
- Failing check: `joinURLPath` (`src/net/http/httputil/reverseproxy.go:305-324`) deliberately concatenates `a.EscapedPath()` and `b.EscapedPath()` to build the outbound `RawPath`, preserving the client's original percent-encoded traversal sequence (`..%2f`) verbatim instead of canonicalizing/cleaning it. There is no call to `path.Clean` or any rejection of `..` segments (encoded or not) in this join operation.
- Consequence: The outbound request's `RawPath`/`Path` still contains the traversal sequence when handed to `Transport.RoundTrip`, so the exact bytes forwarded to the upstream contain `..%2f` relative to the configured base — mirroring the Nitro report where the proxy layer matches/scopes on one interpretation of the path but forwards the raw, un-normalized string to the upstream.

### Impact Explanation
If the upstream behind the Go `ReverseProxy` decodes `%2F` to `/` before further routing or filesystem lookup (the same class of "vulnerable upstream" called out in the Nitro advisory — static file servers, naive path-based dispatchers), the attacker can escape the intended `/base` scope and reach arbitrary paths reachable from that upstream, causing cross-scope disclosure of internal endpoints/files (confidentiality impact, matches CWE-22 path traversal / scope bypass). This would fall under Go's PUBLIC track as a library-level defect enabling path-traversal against consumers of `httputil.ReverseProxy`, contingent on upstream behavior — analogous to Nitro's own conditional-severity framing.

### Likelihood Explanation
Any application using `httputil.NewSingleHostReverseProxy` (or manually calling `rewriteRequestURL`/`joinURLPath` semantics) to scope proxied paths under a base path is exposed to unauthenticated attackers who can simply craft a request with a percent-encoded `..` segment — no special privileges or client trust required, matching the Nitro exploit's unauthenticated `GET` primitive.

### Recommendation
Canonicalize/clean the joined path (decode-then-clean-then-re-encode, or reject any resulting path containing `..` segments after decoding) in `joinURLPath`/`rewriteRequestURL` before constructing the outbound request, and reject requests whose escaped path decodes to a path that would escape the target's base, consistent with the Nitro fix's approach of canonicalizing before forwarding.

### Proof of Concept
```go
package httputil

import (
	"net/url"
	"testing"
)

func TestJoinURLPathPreservesEncodedTraversal(t *testing.T) {
	target, _ := url.Parse("http://upstream.internal/base")
	// Simulate an inbound request whose RawPath contains a percent-encoded
	// traversal segment, as parsed from "/base/..%2Fadmin/secret".
	in := &url.URL{
		Path:    "/base/../admin/secret", // decoded form used for local routing/matching
		RawPath: "/base/..%2Fadmin/secret",
	}

	path, rawpath := joinURLPath(target, in)

	// The raw, un-normalized traversal sequence is preserved and forwarded
	// verbatim to the upstream, even though decoded routing logic would see
	// a normalized/cleaned path.
	if rawpath == "" || rawpath == path {
		t.Fatalf("expected RawPath to retain encoded traversal, got path=%q rawpath=%q", path, rawpath)
	}
	if !contains(rawpath, "..%2F") && !contains(rawpath, "..%2f") {
		t.Fatalf("expected outbound RawPath to still contain percent-encoded traversal, got %q", rawpath)
	}
}

func contains(s, sub string) bool {
	return len(s) >= len(sub) && (func() bool {
		for i := 0; i+len(sub) <= len(s); i++ {
			if s[i:i+len(sub)] == sub {
				return true
			}
		}
		return false
	})()
}
```
Expected assertion: the outbound `RawPath` still contains the encoded `..%2F` sequence, demonstrating that `joinURLPath` forwards the client-controlled percent-encoded traversal unchanged to the upstream rather than normalizing it — the same root-cause primitive as the Nitro `routeRules` proxy scope bypass. [1](#0-0) [2](#0-1) [3](#0-2) 

Note: I was unable to fully inspect `net/http.StripPrefix` and `cleanPath` in `src/net/http/server.go` before the tool budget ran out, so I cannot confirm whether an equivalent or stronger analog exists there (e.g., whether `ServeMux`/`StripPrefix`-based proxy setups perform decoded-path cleaning that diverges from the raw path forwarded downstream). The finding above is based on the confirmed, reachable code in `reverseproxy.go`.

### Citations

**File:** src/net/http/httputil/reverseproxy.go (L305-324)
```go
func joinURLPath(a, b *url.URL) (path, rawpath string) {
	if a.RawPath == "" && b.RawPath == "" {
		return singleJoiningSlash(a.Path, b.Path), ""
	}
	// Same as singleJoiningSlash, but uses EscapedPath to determine
	// whether a slash should be added
	apath := a.EscapedPath()
	bpath := b.EscapedPath()

	aslash := strings.HasSuffix(apath, "/")
	bslash := strings.HasPrefix(bpath, "/")

	switch {
	case aslash && bslash:
		return a.Path + b.Path[1:], apath + bpath[1:]
	case !aslash && !bslash:
		return a.Path + "/" + b.Path, apath + "/" + bpath
	}
	return a.Path + b.Path, apath + bpath
}
```

**File:** src/net/http/httputil/reverseproxy.go (L350-367)
```go
func NewSingleHostReverseProxy(target *url.URL) *ReverseProxy {
	director := func(req *http.Request) {
		rewriteRequestURL(req, target)
	}
	return &ReverseProxy{Director: director}
}

func rewriteRequestURL(req *http.Request, target *url.URL) {
	targetQuery := target.RawQuery
	req.URL.Scheme = target.Scheme
	req.URL.Host = target.Host
	req.URL.Path, req.URL.RawPath = joinURLPath(target, req.URL)
	if targetQuery == "" || req.URL.RawQuery == "" {
		req.URL.RawQuery = targetQuery + req.URL.RawQuery
	} else {
		req.URL.RawQuery = targetQuery + "&" + req.URL.RawQuery
	}
}
```

**File:** src/net/http/httputil/reverseproxy.go (L421-480)
```go
func (p *ReverseProxy) ServeHTTP(rw http.ResponseWriter, req *http.Request) {
	transport := p.Transport
	if transport == nil {
		transport = http.DefaultTransport
	}

	ctx := req.Context()
	if ctx.Done() != nil {
		// CloseNotifier predates context.Context, and has been
		// entirely superseded by it. If the request contains
		// a Context that carries a cancellation signal, don't
		// bother spinning up a goroutine to watch the CloseNotify
		// channel (if any).
		//
		// If the request Context has a nil Done channel (which
		// means it is either context.Background, or a custom
		// Context implementation with no cancellation signal),
		// then consult the CloseNotifier if available.
	} else if cn, ok := rw.(http.CloseNotifier); ok {
		var cancel context.CancelFunc
		ctx, cancel = context.WithCancel(ctx)
		defer cancel()
		notifyChan := cn.CloseNotify()
		go func() {
			select {
			case <-notifyChan:
				cancel()
			case <-ctx.Done():
			}
		}()
	}

	outreq := req.Clone(ctx)
	if req.ContentLength == 0 {
		outreq.Body = nil // Issue 16036: nil Body for http.Transport retries
	}
	if outreq.Body != nil {
		// Reading from the request body after returning from a handler is not
		// allowed, and the RoundTrip goroutine that reads the Body can outlive
		// this handler. This can lead to a crash if the handler panics (see
		// Issue 46866). Although calling Close doesn't guarantee there isn't
		// any Read in flight after the handle returns, in practice it's safe to
		// read after closing it.
		defer outreq.Body.Close()
	}
	if outreq.Header == nil {
		outreq.Header = make(http.Header) // Issue 33142: historical behavior was to always allocate
	}

	if (p.Director != nil) == (p.Rewrite != nil) {
		p.getErrorHandler()(rw, req, errors.New("ReverseProxy must have exactly one of Director or Rewrite set"))
		return
	}

	if p.Director != nil {
		p.Director(outreq)
		if outreq.Form != nil {
			outreq.URL.RawQuery = cleanQueryParams(outreq.URL.RawQuery)
		}
	}
```
