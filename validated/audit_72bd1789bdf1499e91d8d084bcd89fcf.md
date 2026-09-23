### Title
`go env -w`/`go env -u` create the `GOENV` config file with world-readable/writable `0666` permissions, exposing proxy credentials and enabling local config tampering - ([File: src/cmd/go/internal/envcmd/env.go])

### Summary
`cmd/go`'s `go env -w` command persists user configuration (including `GOPROXY`, `GONOSUMCHECK`/`GONOSUMDB`, `GOINSECURE`, `CC`/`CXX`, etc.) to the file returned by `cfg.EnvFile()` (`$XDG_CONFIG_HOME/go/env` by default) using `os.WriteFile(file, data, 0666)`. Under the standard `umask 022`, this yields `-rw-r--r--` (`644`) permissions — the exact world-readable pattern described in the motionEye advisory — and if a system runs with a permissive umask the file remains fully world-writable. This directly contradicts the pattern used elsewhere in the same tree (e.g. `os.ExampleUserConfigDir` at `src/os/example_test.go:346,386` explicitly writes user config with `0600`), showing the secure convention is known but not applied to `GOENV`.

### Finding Description
Local unprivileged user -> reads/writes `$HOME/.config/go/env` (or platform equivalent) -> `envcmd.updateEnvFile` in `src/cmd/go/internal/envcmd/env.go:770-783` calls `os.WriteFile(file, data, 0666)` (falling back to the same `0666` after `os.MkdirAll(..., 0777)` on first creation) -> `os.WriteFile` (`src/os/file.go:939-948`) opens the file with `O_CREATE` and the given `perm`, so the resulting mode is `0666 &^ umask`. Because the file is not created with a restrictive mode like `0600`, any other local account on the same host can read the victim's `GOENV` file (which may contain a `GOPROXY` value embedding HTTP Basic-Auth credentials as `https://user:pass@host`, or private `GOPRIVATE`/`GONOSUMCHECK` settings) via a simple `cat`. If the effective umask is permissive (e.g. `0`, common in container/CI images), the file is also world-writable, letting a local attacker rewrite `CC`, `CXX`, or `GOPROXY` and have malicious code executed the next time the victim runs `go build`/`go env`/`go get`.

### Impact Explanation
Confidentiality: credentials embedded in `GOPROXY` (or other secret-bearing config values a user may store there) are disclosed to any co-resident local user — directly analogous to the motionEye password-hash exposure (CWE-732). Integrity/code-execution: under a lax umask, the same missing restrictive-mode bug also permits local privilege-adjacent tampering (malicious `CC`/`CXX` compiler substitution) executed the next time the go toolchain runs. This would be assessed on the PUBLIC track as a low/medium-severity local-information-disclosure and configuration-integrity issue, since it requires local multi-user access and depends on the value the user chose to store and the umask in effect — narrower than a remote exploit but a legitimate CWE-732 class defect in a "victim workflow" (`go env -w GOPROXY=...` followed by a shared/multi-tenant environment).

### Likelihood Explanation
The victim workflow is ordinary: a developer runs `go env -w GOPROXY=https://user:pass@proxy.example/`, `go env -w GOPRIVATE=...`, or similar on a shared build/CI host. No attacker action is needed to trigger the vulnerable write; any co-located unprivileged local account can then read the file. Exploitation of the write-tampering path additionally depends on the umask, making it a lower but non-zero likelihood scenario (default Linux umask 022 only exposes read, not write).

### Recommendation
Change `updateEnvFile` (and the initial `os.MkdirAll` call) in `src/cmd/go/internal/envcmd/env.go` to create the `GOENV` file (and its parent directory) with restrictive permissions, e.g. `os.WriteFile(file, data, 0600)` and `os.MkdirAll(filepath.Dir(file), 0700)`, matching the pattern already used in `os.ExampleUserConfigDir` (`src/os/example_test.go:384-386`). Consider also tightening permissions on an already-existing, overly permissive file when rewritten.

### Proof of Concept
```go
package envcmd_test

import (
	"os"
	"path/filepath"
	"testing"
)

// Simulates cmd/go/internal/envcmd.updateEnvFile's file creation call.
func TestGoEnvFileIsWorldReadable(t *testing.T) {
	dir := t.TempDir()
	file := filepath.Join(dir, "env")

	data := []byte("GOPROXY=https://user:secret@proxy.example.com\n")

	// Mirrors src/cmd/go/internal/envcmd/env.go:775 & 779
	if err := os.WriteFile(file, data, 0666); err != nil {
		t.Fatal(err)
	}

	info, err := os.Stat(file)
	if err != nil {
		t.Fatal(err)
	}

	// Expect the mode to NOT be more permissive than 0600.
	if perm := info.Mode().Perm(); perm&^0600 != 0 {
		t.Fatalf("go env file has overly permissive mode %v; secrets like GOPROXY basic-auth "+
			"credentials are exposed to other local users", perm)
	}
}
```
Expected: the test fails against current behavior (mode is `0666&^umask`, e.g. `0644`), demonstrating the file is readable by any local user despite containing a credential-bearing `GOPROXY` value; it would pass once the write mode is changed to `0600`. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

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

**File:** src/os/file.go (L934-949)
```go
// WriteFile writes data to the named file, creating it if necessary.
// If the file does not exist, WriteFile creates it with permissions perm (before umask);
// otherwise WriteFile truncates it before writing, without changing permissions.
// Since WriteFile requires multiple system calls to complete, a failure mid-operation
// can leave the file in a partially written state.
func WriteFile(name string, data []byte, perm FileMode) error {
	f, err := OpenFile(name, O_WRONLY|O_CREATE|O_TRUNC, perm)
	if err != nil {
		return err
	}
	_, err = f.Write(data)
	if err1 := f.Close(); err1 != nil && err == nil {
		err = err1
	}
	return err
}
```

**File:** src/os/example_test.go (L380-392)
```go
	if !bytes.Equal(config, origConfig) {
		if configPath == "" {
			log.Printf("not saving config changes: %v", dirErr)
		} else {
			err := os.MkdirAll(filepath.Dir(configPath), 0700)
			if err == nil {
				err = os.WriteFile(configPath, config, 0600)
			}
			if err != nil {
				log.Printf("error saving config changes: %v", err)
			}
		}
	}
```

**File:** src/cmd/go/internal/cfg/cfg.go (L347-364)
```go
// EnvFile returns the name of the Go environment configuration file,
// and reports whether the effective value differs from the default.
func EnvFile() (string, bool, error) {
	if file := os.Getenv("GOENV"); file != "" {
		if file == "off" {
			return "", false, fmt.Errorf("GOENV=off")
		}
		return file, true, nil
	}
	dir, err := os.UserConfigDir()
	if err != nil {
		return "", false, err
	}
	if dir == "" {
		return "", false, fmt.Errorf("missing user-config dir")
	}
	return filepath.Join(dir, "go/env"), false, nil
}
```
