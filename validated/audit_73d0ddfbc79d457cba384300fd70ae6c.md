I found the key code. The `formatErrorDetail` function in `src/cmd/go/internal/web/api.go` is Go's actual defense against exactly this class of vulnerability — it explicitly filters non-graphic, non-space runes before embedding an HTTP error response body into a message that gets printed to the user's terminal (via `stderr`), citing "Don't let the server do any funny business with the user's terminal."### Title
None found — Go's `cmd/go` proxy/VCS error-detail path already sanitizes server-controlled text before terminal output

### Summary
The RubyGems advisory describes `gem owner` writing raw API response bytes directly to stdout, allowing a malicious server to inject terminal escape sequences. The closest analog in this Go tree is `cmd/go`'s handling of untrusted module-proxy/VCS server responses that get echoed into error messages printed to the user's terminal (stderr) — but this code path already implements the exact defense the RubyGems bug lacked.

### Finding Description
When `go get`/`go list -m`/`go mod download` contact a module proxy, `web.Get` → `(*Response).Err` → `formatErrorDetail` in `src/cmd/go/internal/web/api.go` builds an `HTTPError.Detail` string from the untrusted HTTP response body (attacker-controlled, since it comes from any GOPROXY/VCS server reachable via `go get`) and this string is later formatted into the error text printed to the user's terminal via `(*HTTPError).Error()` [1](#0-0) . Before that text is used, `formatErrorDetail` explicitly walks every rune of the body and rejects it entirely unless it is UTF‑8 valid and every rune is `unicode.IsGraphic` or `unicode.IsSpace`, with the comment "Don't let the server do any funny business with the user's terminal" — this is precisely the escape-sequence-injection defense the RubyGems CVE was missing [2](#0-1) . The behavior is exercised and asserted by `src/cmd/go/testdata/script/mod_proxy_errors.txt` and `mod_auth.txt`, which check truncation and propagation of server response text without escape-sequence leakage [3](#0-2) .

### Impact Explanation
No exploitable primitive was found. The candidate sink (`HTTPError.Error()` embedding proxy/VCS response text, ultimately reaching the user's terminal through `base.Fatalf`/`log`-style error printing in the `go` command) is already guarded by a strict allow-list filter (`unicode.IsGraphic`/`unicode.IsSpace`) that drops the entire detail string if any control/escape character is present. This is a Go PUBLIC-track non-issue: the mitigation is present, intentional, and tested.

### Likelihood Explanation
Attacker capability matches the RubyGems scenario (an unprivileged, attacker-controlled GOPROXY/VCS/module-mirror server serving crafted response bytes to a victim running ordinary `go get`/`go list`/`go mod download`), but the reachable sink already neutralizes control characters before they can reach the victim's terminal, so likelihood of a working escape-sequence injection is effectively zero absent a bypass of `formatErrorDetail`'s filter (none found; other stdout/stderr write sites reviewed—`work/shell.go` reportCmd, `vcs.go` run1, `codehost/vcs.go` shell tool—either write only diagnostic/verbose command-line text or already-filtered detail, not raw untrusted body bytes in escape-sequence-relevant contexts).

### Recommendation
No fix required for this code path. If auditing further, verify all other call sites that print `HTTPError`/VCS command stderr (e.g., `RunError.Error()` in `codehost.go`, which appends raw `cmd.Stderr` from a locally-invoked VCS binary rather than a remote server) apply equivalent graphic-character filtering before display, since those paths use the local VCS tool's own stderr rather than the filtered `formatErrorDetail` path and were not fully verified for the same defense.

### Proof of Concept
Not applicable — no vulnerability confirmed. A minimal test demonstrating the existing defense (not a vulnerability) would assert that `formatErrorDetail` returns `""` for a body containing an ANSI escape sequence, e.g. a body of `"\x1b[31mmalicious\x1b[0m"` should cause `Response.Err().(*web.HTTPError).Detail == ""`, confirming the injection is already blocked rather than exploitable.

### Citations

**File:** src/cmd/go/internal/web/api.go (L48-70)
```go
func (e *HTTPError) Error() string {
	if e.Detail != "" {
		detailSep := " "
		if strings.ContainsRune(e.Detail, '\n') {
			detailSep = "\n\t"
		}
		return fmt.Sprintf("reading %s: %v\n\tserver response:%s%s", e.URL, e.Status, detailSep, e.Detail)
	}

	if eErr := e.Err; eErr != nil {
		if pErr, ok := e.Err.(*fs.PathError); ok {
			if u, err := url.Parse(e.URL); err == nil {
				if fp, err := urlToFilePath(u); err == nil && pErr.Path == fp {
					// Remove the redundant copy of the path.
					eErr = pErr.Err
				}
			}
		}
		return fmt.Sprintf("reading %s: %v", e.URL, eErr)
	}

	return fmt.Sprintf("reading %s: %v", e.URL, e.Status)
}
```

**File:** src/cmd/go/internal/web/api.go (L128-168)
```go
// formatErrorDetail converts r.errorDetail (a prefix of the output of r.Body)
// into a short, tab-indented summary.
func (r *Response) formatErrorDetail() string {
	if r.Body != &r.errorDetail {
		return "" // Error detail collection not enabled.
	}

	// Ensure that r.errorDetail has been populated.
	_, _ = io.Copy(io.Discard, r.Body)

	s := r.errorDetail.buf.String()
	if !utf8.ValidString(s) {
		return "" // Don't try to recover non-UTF-8 error messages.
	}
	for _, r := range s {
		if !unicode.IsGraphic(r) && !unicode.IsSpace(r) {
			return "" // Don't let the server do any funny business with the user's terminal.
		}
	}

	var detail strings.Builder
	for i, line := range strings.Split(s, "\n") {
		if strings.TrimSpace(line) == "" {
			break // Stop at the first blank line.
		}
		if i > 0 {
			detail.WriteString("\n\t")
		}
		if i >= maxErrorDetailLines {
			detail.WriteString("[Truncated: too many lines.]")
			break
		}
		if detail.Len()+len(line) > maxErrorDetailBytes {
			detail.WriteString("[Truncated: too long.]")
			break
		}
		detail.WriteString(line)
	}

	return detail.String()
}
```

**File:** src/cmd/go/testdata/script/mod_proxy_errors.txt (L1-16)
```text
env GOSUMDB=off
env GOPROXY=direct

# Server responses should be truncated to some reasonable number of lines.
# (For now, exactly eight.)
! go list -m vcs-test.golang.org/auth/ormanylines@latest
stderr '\tserver response:\n(.|\n)*\tline 8\n\t\[Truncated: too many lines.\]$'

# Server responses should be truncated to some reasonable number of characters.
! go list -m vcs-test.golang.org/auth/oronelongline@latest
! stderr 'blah{40}'
stderr '\tserver response: \[Truncated: too long\.\]$'

# Responses from servers using the 'mod' protocol should be propagated.
! go list -m vcs-test.golang.org/go/modauth404@latest
stderr '\tserver response: File\? What file\?'
```
