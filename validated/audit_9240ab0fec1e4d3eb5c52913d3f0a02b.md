### Title
GOAUTH `git` credential helper output (including plaintext password) is written into error strings that get logged to stderr under `-x` - ([File: src/cmd/go/internal/auth/gitauth.go])

### Summary
`runGitAuth` in `src/cmd/go/internal/auth/gitauth.go` invokes `git credential fill` and captures its combined stdout/stderr with `cmd.CombinedOutput()`. When the helper process exits with a non-zero status, the raw, unredacted `out` bytes — which follow the `git-credential` protocol and can contain `username=...` and `password=...` lines — are embedded verbatim into the returned error via `fmt.Errorf("... %s", ..., out)`. That error is captured in `auth.go`'s `runGoAuth` and, when the user runs `go get -x` / `go mod tidy -x` (i.e. `cfg.BuildX` is set) and no other GOAUTH method supplies a credential for the URL, it is printed to stderr with `log.Printf` at `src/cmd/go/internal/auth/auth.go:131-138`.

### Finding Description
Attacker/victim-workflow input: a `go` command invocation with `GOAUTH=git <dir>` (a supported, documented GOAUTH method) fetching a private module over HTTPS while a configured `git credential` helper fails after emitting credential data (e.g., a helper that logs `username=`/`password=` lines to its combined output before returning non-zero, or one whose underlying store operation errors post-fill). The entry point is `AddCredentials` → `runGoAuth` → `runGitAuth` (`src/cmd/go/internal/auth/gitauth.go:33-51`):
```go
out, err := cmd.CombinedOutput()
if err != nil {
    return "", nil, fmt.Errorf("'git credential fill' failed (url=%s): %w\n%s", url, err, out)
}
```
There is no scrubbing of `out` here — unlike every other place credentials touch logging in this package (e.g. `url.Redacted()` used pervasively in `src/cmd/go/internal/web/http.go`), the failure path does not redact the `password=` field. The error propagates to `runGoAuth` (`src/cmd/go/internal/auth/auth.go:108-115`) into `cmdErrs`, and if `cfg.BuildX && url != ""` and no credential was ultimately loaded, it is dumped with `log.Printf` (`auth.go:134-137`) — the analog of the CVE's "disclosed to stdout/log file" sink.

### Impact Explanation
If triggered, a user's git-credential-helper password (used to authenticate to a private module host) is written in cleartext to the developer's terminal/CI logs during an otherwise ordinary `go get -x`/`go mod tidy -x` invocation, exactly mirroring the Ansible CVE's confidentiality loss of `bind_pw`. This is a data-confidentiality issue, not RCE; on Go's tracks this would sit at most on the PUBLIC/low-severity track since it requires a specific, non-default configuration (`GOAUTH=git`, `-x`, and a misbehaving/erroring credential helper) to actually leak a password rather than benign fill output.

### Likelihood Explanation
Likelihood is low: it requires (1) `GOAUTH` set to the `git` method, (2) the `-x` flag, and (3) the configured `git credential` helper returning a non-zero exit status while its output still contains credential fields — the ordinary success path (`err == nil`) never reaches this code, and a typical failing helper (e.g. "no credential found") would not have emitted a password line at all. This is a narrow, helper-implementation-dependent condition rather than a reliably attacker-triggerable path.

### Recommendation
Do not interpolate the raw `out` from `git credential fill` into error messages. Either omit `out` entirely from the error, or parse it first and only log non-sensitive fields (protocol/host/path), redacting `password=` (and ideally `username=`) before inclusion, consistent with the `url.Redacted()` pattern already used elsewhere in `cmd/go`.

### Proof of Concept
```go
package auth

import (
	"errors"
	"fmt"
	"strings"
	"testing"
)

// Simulates the vulnerable formatting in runGitAuth when 'git credential fill'
// exits non-zero after already emitting a password line.
func TestGitAuthErrorLeaksPassword(t *testing.T) {
	url := "https://example.com/priv/mod"
	simulatedOut := []byte("protocol=https\nhost=example.com\nusername=alice\npassword=s3cr3t\n")
	simulatedErr := errors.New("exit status 1")

	err := fmt.Errorf("'git credential fill' failed (url=%s): %w\n%s", url, simulatedErr, simulatedOut)

	if strings.Contains(err.Error(), "password=s3cr3t") {
		t.Fatalf("credential password leaked into error/log output: %v", err)
	}
}
```
Expected assertion: the test fails today because `err.Error()` contains `password=s3cr3t`, demonstrating that the plaintext credential from `git credential fill`'s output is embedded in a string that `auth.go` will `log.Printf` to stderr under `-x`. [1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** src/cmd/go/internal/auth/gitauth.go (L45-51)
```go
	cmd := exec.Command("git", "credential", "fill")
	cmd.Dir = dir
	cmd.Stdin = strings.NewReader(fmt.Sprintf("url=%s\n", url))
	out, err := cmd.CombinedOutput()
	if err != nil {
		return "", nil, fmt.Errorf("'git credential fill' failed (url=%s): %w\n%s", url, err, out)
	}
```

**File:** src/cmd/go/internal/auth/auth.go (L108-139)
```go
			prefix, header, err := runGitAuth(client, dir, url)
			if err != nil {
				// Save the error, but don't print it yet in case another
				// GOAUTH command might succeed.
				cmdErrs = append(cmdErrs, fmt.Errorf("GOAUTH=%s: %v", command, err))
			} else {
				storeCredential(prefix, header)
			}
		default:
			credentials, err := runAuthCommand(command, url, res)
			if err != nil {
				// Save the error, but don't print it yet in case another
				// GOAUTH command might succeed.
				cmdErrs = append(cmdErrs, fmt.Errorf("GOAUTH=%s: %v", command, err))
				continue
			}
			for prefix := range credentials {
				storeCredential(prefix, credentials[prefix])
			}
		}
	}
	// If no GOAUTH command provided a credential for the given url
	// and an error occurred, log the error.
	if cfg.BuildX && url != "" {
		req := &http.Request{Header: make(http.Header)}
		if ok := loadCredential(req, url); !ok && len(cmdErrs) > 0 {
			log.Printf("GOAUTH encountered errors for %s:", url)
			for _, err := range cmdErrs {
				log.Printf("  %v", err)
			}
		}
	}
```

**File:** src/cmd/go/internal/web/http.go (L113-122)
```go
	fetch := func(url *urlpkg.URL) (*http.Response, error) {
		// Note: The -v build flag does not mean "print logging information",
		// despite its historical misuse for this in GOPATH-based go get.
		// We print extra logging in -x mode instead, which traces what
		// commands are executed.
		if cfg.BuildX {
			fmt.Fprintf(os.Stderr, "# get %s\n", url.Redacted())
		}

		req, err := http.NewRequest("GET", url.String(), nil)
```
