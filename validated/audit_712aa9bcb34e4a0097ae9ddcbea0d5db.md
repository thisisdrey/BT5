#No vulnerability found for this question.

The Kimai advisory describes a coarse-grained authorization check (`own` vs `other`) that Kimai's maintainers confirmed is **documented, by-design behavior**, not a broken control. Searching this Go repository for an analogous "voter"/permission-scoping construct, the closest candidate is `net/http.CrossOriginProtection` and its `Check`/`isRequestExempt` logic [1](#0-0) , which enforces origin-scoped access via `Sec-Fetch-Site`, `Origin`/`Host` comparison, exact-match trusted origins, and `ServeMux`-based bypass patterns [2](#0-1) .

Unlike the Kimai voter, this check does not conflate identity scopes — `AddTrustedOrigin` requires an exact origin match [3](#0-2) , and the "same-origin"/no-header fallback behavior is explicitly documented as an accepted design trade-off rather than a missing scope check [4](#0-3) . I found no Go entry point in this repository where a permission/authorization check silently substitutes a broader scope for a narrower one in a way that contradicts its own documented behavior or fails an intended security boundary, so there is no reproducible analog to the reported Kimai bug class in this codebase.

### Citations

**File:** src/net/http/csrf.go (L22-27)
```go
// The GET, HEAD, and OPTIONS methods are [safe methods] and are always allowed.
// It's important that applications do not perform any state changing actions
// due to requests with safe methods.
//
// Requests without Sec-Fetch-Site or Origin headers are currently assumed to be
// either same-origin or non-browser requests, and are allowed.
```

**File:** src/net/http/csrf.go (L57-78)
```go
func (c *CrossOriginProtection) AddTrustedOrigin(origin string) error {
	u, err := url.Parse(origin)
	if err != nil {
		return fmt.Errorf("invalid origin %q: %w", origin, err)
	}
	if u.Scheme == "" {
		return fmt.Errorf("invalid origin %q: scheme is required", origin)
	}
	if u.Host == "" {
		return fmt.Errorf("invalid origin %q: host is required", origin)
	}
	if u.User != nil || u.Path != "" || u.RawQuery != "" || u.Fragment != "" {
		return fmt.Errorf("invalid origin %q: userinfo, path, query, and fragment are not allowed", origin)
	}
	c.trustedMu.Lock()
	defer c.trustedMu.Unlock()
	if c.trusted == nil {
		c.trusted = make(map[string]bool)
	}
	c.trusted[origin] = true
	return nil
}
```

**File:** src/net/http/csrf.go (L134-175)
```go
func (c *CrossOriginProtection) Check(req *Request) error {
	switch req.Method {
	case "GET", "HEAD", "OPTIONS":
		// Safe methods are always allowed.
		return nil
	}

	switch req.Header.Get("Sec-Fetch-Site") {
	case "":
		// No Sec-Fetch-Site header is present.
		// Fallthrough to check the Origin header.
	case "same-origin", "none":
		return nil
	default:
		if c.isRequestExempt(req) {
			return nil
		}
		return errCrossOriginRequest
	}

	origin := req.Header.Get("Origin")
	if origin == "" {
		// Neither Sec-Fetch-Site nor Origin headers are present.
		// Either the request is same-origin or not a browser request.
		return nil
	}

	if o, err := url.Parse(origin); err == nil && o.Host == req.Host {
		// The Origin header matches the Host header. Note that the Host header
		// doesn't include the scheme, so we don't know if this might be an
		// HTTP→HTTPS cross-origin request. We fail open, since all modern
		// browsers support Sec-Fetch-Site since 2023, and running an older
		// browser makes a clear security trade-off already. Sites can mitigate
		// this with HTTP Strict Transport Security (HSTS).
		return nil
	}

	if c.isRequestExempt(req) {
		return nil
	}
	return errCrossOriginRequestFromOldBrowser
}
```

**File:** src/net/http/csrf.go (L183-198)
```go
// isRequestExempt checks the bypasses which require taking a lock, and should
// be deferred until the last moment.
func (c *CrossOriginProtection) isRequestExempt(req *Request) bool {
	if bypass := c.bypass.Load(); bypass != nil {
		if h, _ := bypass.Handler(req); h == sentinelHandler {
			// The request matches a bypass pattern.
			return true
		}
	}

	c.trustedMu.RLock()
	defer c.trustedMu.RUnlock()
	origin := req.Header.Get("Origin")
	// The request matches a trusted origin.
	return origin != "" && c.trusted[origin]
}
```
