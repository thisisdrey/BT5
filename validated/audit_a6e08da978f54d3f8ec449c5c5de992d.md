### Title
Go environment config file written world-readable (0666), exposing `GOPROXY` credentials to local users - (File: `src/cmd/go/internal/envcmd/env.go`)

### Summary
`go env -w` persists user-supplied `KEY=VALUE` settings — including `GOPROXY`, which the Go module documentation explicitly allows to embed HTTP Basic Auth credentials as `https://user:pass@proxy.example.com` — into a per-user config file using `os.WriteFile(file, data, 0666)`. Because the mode is `0666` "before umask" rather than a restrictive mode like `0600`, on typical Unix systems with the default `umask 022` the resulting file is created world-readable (`0644`), letting any local, unprivileged user on the same host read another user's `go env` file and recover embedded proxy credentials. This is the same bug class as CVE-2014-4658 (Ansible vault): a locally-readable file holding secrets because file creation relied on a permissive default mode instead of an explicit restrictive one.

### Finding Description
Entry point: `go env -w GOPROXY=https://user:pass@proxy.internal` (also usable for any credential-bearing value a user may put in a GOFLAGS/GOPROXY/GOPRIVATE-style variable). This calls `runEnvW` → `updateEnvFile` in [1](#0-0)  which builds the new config content and writes it with: [2](#0-1) 
There is no explicit `Chmod` to a restrictive mode and no use of `0600` (contrast with the pattern used elsewhere in the standard library for per-user config files, e.g. `os.WriteFile(configPath, config, 0600)` in [3](#0-2) ). The env file itself is documented as living in the per-user config directory (`os.UserConfigDir`), which is meant to be private to the user, but the file's own permissions do not enforce that — they rely entirely on the process umask, exactly the pattern that caused CVE-2014-4658 in Ansible's vault file creation. `GOPROXY` is separately documented to support proxy authentication via credentials in the URL and is read back and used by `modfetch/proxy.go`'s `proxyList()` ( [4](#0-3) ), confirming the value legitimately can and does carry secrets.

### Impact Explanation
On a shared multi-user machine, any other local unprivileged user can read `$HOME/.config/go/env` (or the OS-specific equivalent) once it has been created/updated by `go env -w GOPROXY=...`, exposing the victim's private module-proxy credentials (CWE-200, matches the referenced advisory's C:H/I:N/A:N profile). This is a local information-disclosure primitive, not remote code execution, so it would fall under Go's PRIVATE/lower-severity track rather than PUBLIC/URGENT, but it is a genuine, reachable credential-disclosure bug in supported release code, not test/mock/vendored code.

### Likelihood Explanation
Any developer who configures a private/authenticated module proxy via `go env -w GOPROXY=https://user:pass@host` on a shared or multi-tenant system (common in CI runners, shared build servers, or shared dev boxes) triggers this write path. No attacker action beyond having a local, unprivileged account on the same host and standard file read permissions is required — matching the CWE-200/local-readability primitive of the analog report.

### Recommendation
Create the Go env config file with an explicit restrictive mode (e.g. `0600`) instead of `0666`, and/or `Chmod` it to `0600` after creation in `updateEnvFile`; likewise document that values placed in the env file (like credential-bearing `GOPROXY` URLs) require a private-mode file regardless of the process umask.

### Proof of Concept
```go
package envcmd_test

import (
	"os"
	"path/filepath"
	"testing"
)

// Simulates updateEnvFile's write call to show the resulting mode is
// world-readable under a standard umask, exposing embedded GOPROXY credentials.
func TestGoEnvFileIsWorldReadable(t *testing.T) {
	dir := t.TempDir()
	file := filepath.Join(dir, "env")

	oldUmask := syscallUmask(0o022) // typical default umask
	defer syscallUmask(oldUmask)

	data := []byte("GOPROXY=https://user:secretpassword@proxy.internal\n")
	if err := os.WriteFile(file, data, 0666); err != nil {
		t.Fatal(err)
	}

	fi, err := os.Stat(file)
	if err != nil {
		t.Fatal(err)
	}
	mode := fi.Mode().Perm()
	if mode&0o044 != 0 {
		t.Fatalf("go env file is readable by group/other (mode %o); embedded GOPROXY credentials are locally disclosed", mode)
	}
}
```
Expected result: the test fails, showing mode `0644` (world-readable), demonstrating that credentials embedded in `GOPROXY` written via `go env -w` are exposed to any local user, analogous to the Ansible vault umask disclosure (CVE-2014-4658).

### Citations

**File:** src/cmd/go/internal/envcmd/env.go (L726-751)
```go
func updateEnvFile(add map[string]string, del map[string]bool) {
	lines := readEnvFileLines(len(add) == 0)

	// Delete all but last copy of any duplicated variables,
	// since the last copy is the one that takes effect.
	prev := make(map[string]int)
	for l, line := range lines {
		if key := lineToKey(line); key != "" {
			if p, ok := prev[key]; ok {
				lines[p] = ""
			}
			prev[key] = l
		}
	}

	// Add variables (go env -w). Update existing lines in file if present, add to end otherwise.
	for key, val := range add {
		if p, ok := prev[key]; ok {
			lines[p] = key + "=" + val + "\n"
			delete(add, key)
		}
	}
	for key, val := range add {
		lines = append(lines, key+"="+val+"\n")
	}

```

**File:** src/cmd/go/internal/envcmd/env.go (L770-784)
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
}
```

**File:** src/os/example_test.go (L380-391)
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
```

**File:** src/cmd/go/internal/modfetch/proxy.go (L61-80)
```go
func proxyList() ([]proxySpec, error) {
	proxyOnce.Do(func() {
		if cfg.GONOPROXY != "" && cfg.GOPROXY != "direct" {
			proxyOnce.list = append(proxyOnce.list, proxySpec{url: "noproxy"})
		}

		goproxy := cfg.GOPROXY
		for goproxy != "" {
			var url string
			fallBackOnError := false
			if i := strings.IndexAny(goproxy, ",|"); i >= 0 {
				url = goproxy[:i]
				fallBackOnError = goproxy[i] == '|'
				goproxy = goproxy[i+1:]
			} else {
				url = goproxy
				goproxy = ""
			}

			url = strings.TrimSpace(url)
```
