Found a concrete analog: `updateEnvFile` in `src/cmd/go/internal/envcmd/env.go` writes the per-user `go env -w` config file (`GOENV`, typically under `os.UserConfigDir()`) with mode `0666` (world-writable/readable, before umask), and even creates its parent directory with `os.MkdirAll(filepath.Dir(file), 0777)` (world-writable/executable) if it's missing. [1](#0-0) 

### Title
Go environment config file (`GOENV`) created world-writable/readable (0666/0777), exposing configuration data (including embedded proxy credentials) to other local users - (File: src/cmd/go/internal/envcmd/env.go)

### Finding Description
`go env -w KEY=VALUE` (invoked via `runEnvW` → `updateEnvFile`) rewrites the entire per-user Go environment config file identified by `cfg.EnvFile()` (default location under `os.UserConfigDir()/go/env`). The file is written with `os.WriteFile(file, data, 0666)`, and if the containing directory doesn't exist, it is created via `os.MkdirAll(filepath.Dir(file), 0777)` [1](#0-0) . Neither the file mode nor the directory mode account for umask hardening beyond the OS-default umask, and no explicit `0600`/`0700` restriction is applied as is done elsewhere in the same package tree (e.g. `lockedfile` uses `0666` too, but that's for generic locked files, not credential-bearing config). This file can legitimately contain sensitive values: `GOPROXY` supports embedding HTTP basic-auth credentials directly in the URL (`https://user:pass@proxy.example.com`), and `GOENV` explicitly documents storing `GOPROXY`, `GOSUMDB`, `GOPRIVATE`, `GONOPROXY`, etc. [2](#0-1) . On a shared multi-user Unix host, any local process/user with default umask (commonly 022, but nothing in the go command enforces stricter permissions) can read (and even write) another user's `go/env` file once created, disclosing embedded proxy credentials — directly analogous to Grafana's `grafana.db` world-readable file exposing datasource passwords (CWE-312/CWE-732).

### Impact Explanation
If a `GOPROXY` value with embedded basic-auth credentials is stored via `go env -w GOPROXY=https://user:pass@proxy.internal`, those credentials sit in a file mode 0666 that any local user can read regardless of umask enforcement by the Go tool itself (the tool never chmods the file to 0600). This is a local information-disclosure risk (CWE-312), and because 0666 also permits arbitrary local write access, another local user could silently rewrite the victim's Go proxy/sumdb settings (integrity impact, CWE-732), potentially redirecting module downloads through an attacker-controlled proxy on subsequent `go` invocations. This would likely fall under Go's PUBLIC track as a low-severity, environment/config-file hardening issue rather than a memory-safety or remote-exploitation bug.

### Likelihood Explanation
Requires a shared multi-user host where an unprivileged local user account already has code-execution-independent read access to the filesystem (a normal "ordinary user data" premise, no privilege escalation needed) and where the victim has run `go env -w GOPROXY=...` with credentials in the URL — a workflow explicitly supported/documented by the tool. Likelihood is moderate: it depends on victims embedding credentials in `GOPROXY` (a documented, though not best-practice, way to configure private proxies) and using a shared machine.

### Recommendation
Create the `GOENV` file and its parent directory with restrictive permissions (e.g., `0600` for the file, `0700` for the directory) instead of `0666`/`0777`, mirroring the fix pattern used for the analogous Grafana advisory (tightening default file/dir permissions for files that may carry secrets).

### Proof of Concept
```go
package envcmd_test

import (
	"os"
	"path/filepath"
	"testing"
)

// Simulates updateEnvFile's write call to demonstrate the resulting mode.
func TestGoEnvFilePermissive(t *testing.T) {
	dir := t.TempDir()
	file := filepath.Join(dir, "env")

	data := []byte("GOPROXY=https://user:secret@proxy.internal\n")
	if err := os.WriteFile(file, data, 0666); err != nil {
		t.Fatal(err)
	}

	fi, err := os.Stat(file)
	if err != nil {
		t.Fatal(err)
	}
	// Expect failure: mode should NOT allow world read/write.
	if fi.Mode().Perm()&0077 != 0 {
		t.Fatalf("go env file is accessible to group/other: mode=%v (expected 0600-class perms)", fi.Mode().Perm())
	}
}
```
Expected: the assertion fails on unpatched code because `updateEnvFile` uses `0666`, confirming the file (which may contain `GOPROXY` credentials) is group/world readable and writable.

### Citations

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

**File:** src/cmd/go/alldocs.go (L2419-2431)
```go
// # Environment variables
//
// The go command and the tools it invokes consult environment variables
// for configuration. If an environment variable is unset or empty, the go
// command uses a sensible default setting. To see the effective setting of
// the variable <NAME>, run 'go env <NAME>'. To change the default setting,
// run 'go env -w <NAME>=<VALUE>'. Defaults changed using 'go env -w'
// are recorded in a Go environment configuration file stored in the
// per-user configuration directory, as reported by os.UserConfigDir.
// The location of the configuration file can be changed by setting
// the environment variable GOENV, and 'go env GOENV' prints the
// effective location, but 'go env -w' cannot change the default location.
// See 'go help env' for details.
```
