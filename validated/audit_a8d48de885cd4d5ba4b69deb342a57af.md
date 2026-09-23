### Title
Credential Disclosure via Process-Listing when go get/go mod fetches over Git with URL-Embedded Credentials - (File: src/cmd/go/internal/modfetch/codehost/git.go)

### Summary
When the `go` command fetches a module or import path over a direct Git remote whose URL embeds HTTP Basic-Auth credentials (`https://user:token@host/repo`), that raw URL string — including the plaintext credentials — is passed as a literal `argv` element to the `git` subprocess spawned via `exec.Command`/`exec.CommandContext`. On Unix-like systems any other local user can read another process's full command line (e.g. via `ps auxww` or `/proc/<pid>/cmdline`), which discloses the embedded username/password for the duration the `git` subprocess runs. This mirrors the darkhttpd CVE-2024-23770 pattern of exposing secrets passed via command-line arguments.

### Finding Description
Attacker input: none required from the attacker beyond being an unprivileged local user on the same host as the victim running `go get`/`go mod download`/`go build` against a private repository whose remote URL contains embedded Basic-Auth credentials.

Entry point: `newGitRepo` in `src/cmd/go/internal/modfetch/codehost/git.go` receives `remote` (the raw repository URL, which may contain a `user:password@` userinfo component per RFC 3986/`net/url`). This value is then forwarded verbatim as a command-line argument to the `git` binary in multiple places:

- `r.runGit(ctx, "git", "remote", "add", "origin", "--", r.remote)` [1](#0-0) 
- `r.runGit(ctx, "git", "ls-remote", "-q", "--end-of-options", r.remote)` [2](#0-1) 
- `r.runGit(ctx, "git", "-c", "protocol.version=2", "fetch", "-f", "--depth=1", "--end-of-options", r.remote, refspec)` [3](#0-2) 

These calls funnel into `codehost.Run`, which builds an `exec.CommandContext(ctx, cmd[0], cmd[1:]...)` where `cmd` is the full string slice including the remote URL, and starts the process with `c.Run()`: [4](#0-3) 

The `-x`/verbose logging path in the same function even writes the command line (containing the credential-bearing URL) to a log writer, compounding exposure, though the core issue is the OS-level argv exposure regardless of `-x`. [5](#0-4) 

Failed check: nowhere in this path is the URL's userinfo stripped, redacted, or passed via an out-of-band channel (e.g., a Git credential helper, environment variable, or `GIT_ASKPASS`) before being placed into `cmd.Args`. Contrast this with `net/url.URL.Redacted()`, which the `go` command already uses elsewhere (e.g. `insecure.Redacted()`, `p.url.Redacted()` in `modfetch/proxy.go`) specifically to avoid printing credentials — but that redaction is applied only to *displayed/logged* URLs, not to the URL actually handed to the `git` subprocess's argv. [6](#0-5) 

Sink: `os/exec.Cmd.Args`/`Path`, ultimately passed to the OS `exec` syscall, whose argv is readable by any local user with permission to inspect `/proc/<pid>/cmdline` or run `ps`. [7](#0-6) 

### Impact Explanation
Any other unprivileged local user on the same machine can observe Basic-Auth credentials (username/password or token) embedded in a private Git remote URL while `go get`/`go mod download`/`go build` (with module resolution needing a direct VCS fetch) is executing, by listing processes. This is a local information-disclosure of credentials analogous to CVE-2024-23770 — a Medium-severity, PUBLIC-track class of issue (local, unprivileged, requires the specific victim workflow of using a credential-in-URL Git remote and process listing at the right time).

### Likelihood Explanation
Requires: (1) the victim configures/fetches a private module or import path via a direct Git remote URL that embeds credentials (a discouraged but not-prohibited practice, and explicitly what `Redacted()` calls elsewhere are designed to hide from logs) and (2) an unprivileged local attacker capable of listing processes on the same host during the fetch. `go`'s own documented, preferred mechanisms (`GOAUTH`, `netrc`, `git credential fill`) avoid this exact exposure by passing secrets via stdin/HTTP headers/credential helpers rather than argv, showing the project is aware of and mitigates this class elsewhere — but the direct-VCS git.go path with URL-embedded credentials is not covered by that mitigation.

### Recommendation
Before passing `remote` to `git` subprocess invocations in `git.go`, detect and strip userinfo from the URL, and instead supply credentials via a Git credential helper, `GIT_ASKPASS`, or environment-based mechanism (consistent with the existing `GOAUTH`/`git credential fill` support in `cmd/go/internal/auth`). At minimum, reject or warn on URL-embedded credentials for VCS-direct fetches the same way `web/http.go` refuses "insecure credentials" over an HTTP URL, and never place the raw credential-bearing string into `exec.Cmd.Args`.

### Proof of Concept
```go
package git_test

import (
	"os/exec"
	"strings"
	"testing"
)

// Minimal analog test: simulate how codehost.Run/exec.Command constructs argv
// from a remote URL that embeds Basic-Auth credentials, and show those
// credentials end up in the resulting Cmd.Args (and thus in the OS
// process table / /proc/<pid>/cmdline, visible to other local users).
func TestCredentialLeakInGitArgv(t *testing.T) {
	remote := "https://alice:s3cr3t-token@example.com/private/repo.git"

	cmd := exec.Command("git", "ls-remote", "-q", "--end-of-options", remote)

	full := strings.Join(cmd.Args, " ")
	if !strings.Contains(full, "s3cr3t-token") {
		t.Fatalf("expected credential to be present in argv for repro, got: %q", full)
	}
	// Assertion demonstrating the vulnerability: the secret is present
	// in the process's command-line arguments, which any local user can
	// read via `ps` or /proc/<pid>/cmdline while the process runs.
	t.Logf("argv exposing credential: %s", full)
}
```
Expected assertion: the test passes, demonstrating that the credential (`s3cr3t-token`) is embedded directly in `cmd.Args`, confirming that any local process listing during `go`'s invocation of `git ls-remote`/`git remote add`/`git fetch` with such a URL would disclose it — the same class of exposure described in CVE-2024-23770 for `darkhttpd --auth`.

### Citations

**File:** src/cmd/go/internal/modfetch/codehost/git.go (L119-119)
```go
		if _, err := r.runGit(ctx, "git", "remote", "add", "origin", "--", r.remote); err != nil {
```

**File:** src/cmd/go/internal/modfetch/codehost/git.go (L264-264)
```go
		out, gitErr := r.runGit(ctx, "git", "ls-remote", "-q", "--end-of-options", r.remote)
```

**File:** src/cmd/go/internal/modfetch/codehost/git.go (L582-582)
```go
		_, err = r.runGit(ctx, "git", "-c", "protocol.version=2", "fetch", "-f", "--depth=1", "--end-of-options", r.remote, refspec)
```

**File:** src/cmd/go/internal/modfetch/codehost/codehost.go (L328-387)
```go
func run(ctx context.Context, args RunArgs) ([]byte, error) {
	if args.dir != "" {
		muIface, ok := dirLock.Load(args.dir)
		if !ok {
			muIface, _ = dirLock.LoadOrStore(args.dir, new(sync.Mutex))
		}
		mu := muIface.(*sync.Mutex)
		mu.Lock()
		defer mu.Unlock()
	}

	cmd := str.StringList(args.cmdline...)
	if xLog, ok := cfg.BuildXWriter(ctx); ok {
		text := new(strings.Builder)
		if args.dir != "" {
			text.WriteString("cd ")
			text.WriteString(args.dir)
			text.WriteString("; ")
		}
		for i, arg := range cmd {
			if i > 0 {
				text.WriteByte(' ')
			}
			switch {
			case strings.ContainsAny(arg, "'"):
				// Quote args that could be mistaken for quoted args.
				text.WriteByte('"')
				text.WriteString(bashQuoter.Replace(arg))
				text.WriteByte('"')
			case strings.ContainsAny(arg, "$`\\*?[\"\t\n\v\f\r \u0085\u00a0"):
				// Quote args that contain special characters, glob patterns, or spaces.
				text.WriteByte('\'')
				text.WriteString(arg)
				text.WriteByte('\'')
			default:
				text.WriteString(arg)
			}
		}
		fmt.Fprintf(xLog, "%s\n", text)
		start := time.Now()
		defer func() {
			fmt.Fprintf(xLog, "%.3fs # %s\n", time.Since(start).Seconds(), text)
		}()
	}
	// TODO: Impose limits on command output size.
	// TODO: Set environment to get English error messages.
	var stderr bytes.Buffer
	var stdout bytes.Buffer
	c := exec.CommandContext(ctx, cmd[0], cmd[1:]...)
	c.Cancel = func() error { return c.Process.Signal(os.Interrupt) }
	c.Dir = args.dir
	c.Stdin = args.stdin
	c.Stderr = &stderr
	c.Stdout = &stdout
	c.Env = append(c.Environ(), args.env...)
	err := c.Run()
	if err != nil {
		err = &RunError{Cmd: strings.Join(cmd, " ") + " in " + args.dir, Stderr: stderr.Bytes(), Err: err}
	}
	return stdout.Bytes(), err
```

**File:** src/cmd/go/internal/modfetch/proxy.go (L196-226)
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

	// Append the module path to the URL.
	url := base
	enc, err := module.EscapePath(path)
	if err != nil {
		return nil, err
	}
	url.Path = strings.TrimSuffix(base.Path, "/") + "/" + enc
	url.RawPath = strings.TrimSuffix(base.RawPath, "/") + "/" + pathEscape(enc)

	return &proxyRepo{url, path, redactedBase, sync.Once{}, nil, nil}, nil
}
```

**File:** src/os/exec/exec.go (L415-419)
```go
func Command(name string, arg ...string) *Cmd {
	cmd := &Cmd{
		Path: name,
		Args: append([]string{name}, arg...),
	}
```
