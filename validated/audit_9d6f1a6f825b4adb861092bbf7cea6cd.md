### Title
Go env config file (`go/env`) written with world-readable 0666 permissions can expose GOPROXY credentials to other local users - (File: src/cmd/go/internal/envcmd/env.go)

### Summary
The `go env -w` command persists user-supplied `KEY=VALUE` settings — including `GOPROXY` URLs that can embed HTTP Basic-Auth credentials (`https://user:pass@proxy.example.com`) — into the per-user Go environment configuration file. `updateEnvFile` writes this file with `os.WriteFile(file, data, 0666)` and, if the containing directory is missing, creates it with `os.MkdirAll(filepath.Dir(file), 0777)`, without ever tightening permissions to something private like `0600`. On multi-user Unix systems this mirrors the AWS CLI `cli_history` finding: a local file that can hold sensitive credentials is created with default, non-restrictive permissions, letting other local users read it.

### Finding Description
A user runs `go env -w GOPROXY=https://user:pass@proxy.example.com` (a normal, documented workflow — `GOPROXY` URLs may contain userinfo, which `net/url.parseAuthority` at `src/net/url/url.go:522-555` fully supports, and `newProxyRepo` in `src/cmd/go/internal/modfetch/proxy.go:196-226` accepts such URLs verbatim). This reaches `runEnvW` → `updateEnvFile` in `src/cmd/go/internal/envcmd/env.go:726-784`. There, `checkEnvWrite` (`src/cmd/go/internal/envcmd/env.go:632-703`) validates the value's syntax (UTF-8, no NUL/newline) but performs no secrecy handling, and the final write:
```go
data := []byte(strings.Join(lines, ""))
err = os.WriteFile(file, data, 0666)
...
os.MkdirAll(filepath.Dir(file), 0777)
err = os.WriteFile(file, data, 0666)
```
(`src/cmd/go/internal/envcmd/env.go:774-783`) creates or rewrites the config file with mode `0666` (world read/write before umask) and the parent directory with `0777`. `os.WriteFile`'s own doc (`src/os/file.go:934-938`) states that if the file already exists, permissions are *not* changed on subsequent writes, so even upgrading Go or re-running `go env -w` won't retroactively fix an already-permissive file. On typical Linux umask (022), the resulting file is `0644` — readable by every local user — while credentials embedded in `GOPROXY` (or similar sensitive values a user might place in this file) remain exposed to any other local account.

### Impact Explanation
Any other local, unprivileged user account on the same multi-user Unix host can read `$XDG_CONFIG_HOME/go/env` (or the platform equivalent) and recover embedded module-proxy credentials, disclosing secrets that were only intended for the file's owner (CWE-276, matches the referenced GHSA-747p-wmpv-9c78 class). This is a confidentiality-only, local-disclosure issue with no code execution, matching Go's PUBLIC track severity bucket for permission-hardening issues (not urgent/security-release class, but a legitimate hardening bug).

### Likelihood Explanation
The workflow is the officially documented mechanism (`go help environment`, `go env -w`) for configuring private/authenticated module proxies, so any developer relying on `GOPROXY=https://user:pass@...` on a shared build server or CI Unix box will trigger this. The attacker only needs an ordinary, unprivileged local account on the same machine — no code execution, credentials, or environment control is required.

### Recommendation
Create the `go/env` file (and its parent directory) with restrictive permissions on Unix, e.g. `0600`/`0700`, and enforce this even for existing files (equivalent to `os.OpenFile` with `O_CREATE` and an explicit `Chmod`/`umask`-independent enforcement), analogous to how `awscli` now creates its history database with owner-only permissions.

### Proof of Concept
```go
package envcmd_test

import (
	"os"
	"path/filepath"
	"testing"
)

// Simulates updateEnvFile's write call to show the resulting mode is
// group/world readable regardless of umask expectations for a secrets file.
func TestGoEnvFilePermissions(t *testing.T) {
	dir := t.TempDir()
	file := filepath.Join(dir, "env")

	data := []byte("GOPROXY=https://user:pass@proxy.example.com\n")
	if err := os.WriteFile(file, data, 0666); err != nil {
		t.Fatal(err)
	}

	fi, err := os.Stat(file)
	if err != nil {
		t.Fatal(err)
	}
	if fi.Mode().Perm()&0044 != 0 {
		t.Fatalf("go env file is group/world readable: %v (want 0600-like perms for a file that may contain credentials)", fi.Mode().Perm())
	}
}
```
Expected: the assertion fails on a default umask (e.g. 022), showing the file is created `0644`, confirming the credential-bearing config file is readable by other local users. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** src/cmd/go/internal/envcmd/env.go (L632-655)
```go
func checkEnvWrite(key, val string) error {
	switch key {
	case "GOEXE",
		"GOGCCFLAGS",
		"GOHOSTARCH",
		"GOHOSTOS",
		"GOMOD",
		"GOROOT",
		"GOTELEMETRY",
		"GOTELEMETRYDIR",
		"GOTOOLDIR",
		"GOVERSION",
		"GOWORK":
		return fmt.Errorf("%s cannot be modified", key)
	case "GOENV", "GODEBUG":
		return fmt.Errorf("%s can only be set using the OS environment", key)
	}

	// To catch typos and the like, check that we know the variable.
	// If it's already in the env file, we assume it's known.
	if !cfg.CanGetenv(key) {
		return fmt.Errorf("unknown go command variable %s", key)
	}

```

**File:** src/cmd/go/internal/envcmd/env.go (L770-783)
```go
	file, _, err := cfg.EnvFile()
	if file == "" {
		base.Fatalf("go: cannot find go env config: %v", err)
	}
	data := []byte(strings.Join(lines, ""))
	err = os.WriteFile(file, data, 0666)
	if err != nil {
		// Try creating directory.
		os.MkdirAll(filepath.Dir(file), 0777)
		err = os.WriteFile(file, data, 0666)
		if err != nil {
			base.Fatalf("go: writing go env config: %v", err)
		}
	}
```

**File:** src/net/url/url.go (L522-554)
```go
func parseAuthority(scheme, authority string) (user *Userinfo, host string, err error) {
	i := strings.LastIndex(authority, "@")
	if i < 0 {
		host, err = parseHost(scheme, authority)
	} else {
		host, err = parseHost(scheme, authority[i+1:])
	}
	if err != nil {
		return nil, "", err
	}
	if i < 0 {
		return nil, host, nil
	}
	userinfo := authority[:i]
	if !validUserinfo(userinfo) {
		return nil, "", errors.New("net/url: invalid userinfo")
	}
	if !strings.Contains(userinfo, ":") {
		if userinfo, err = unescape(userinfo, encodeUserPassword); err != nil {
			return nil, "", err
		}
		user = User(userinfo)
	} else {
		username, password, _ := strings.Cut(userinfo, ":")
		if username, err = unescape(username, encodeUserPassword); err != nil {
			return nil, "", err
		}
		if password, err = unescape(password, encodeUserPassword); err != nil {
			return nil, "", err
		}
		user = UserPassword(username, password)
	}
	return user, host, nil
```

**File:** src/cmd/go/internal/modfetch/proxy.go (L196-215)
```go
func newProxyRepo(baseURL, path string) (Repo, error) {
	// Parse the base proxy URL.
	base, err := url.Parse(baseURL)
	if err != nil {
		return nil, err
	}
	redactedBase := base.Redacted()
	switch base.Scheme {
	case "http", "https":
		// ok
	case "file":
		if *base != (url.URL{Scheme: base.Scheme, Path: base.Path, RawPath: base.RawPath}) {
			return nil, fmt.Errorf("invalid file:// proxy URL with non-path elements: %s", redactedBase)
		}
	case "":
		return nil, fmt.Errorf("invalid proxy URL missing scheme: %s", redactedBase)
	default:
		return nil, fmt.Errorf("invalid proxy URL scheme (must be https, http, file): %s", redactedBase)
	}

```

**File:** src/os/file.go (L934-939)
```go
// WriteFile writes data to the named file, creating it if necessary.
// If the file does not exist, WriteFile creates it with permissions perm (before umask);
// otherwise WriteFile truncates it before writing, without changing permissions.
// Since WriteFile requires multiple system calls to complete, a failure mid-operation
// can leave the file in a partially written state.
func WriteFile(name string, data []byte, perm FileMode) error {
```
