### Title
Unsanitized SVN file names permit path traversal in `svnReadZip` - ([File: src/cmd/go/internal/modfetch/codehost/svn.go])

### Summary
`svnReadZip` builds a module zip file by combining a file list from `svn list --xml` with a local export produced by `svn export`, then reads each listed file with `os.Open(filepath.Join(exportDir, e.Name))`. The `Name` field comes directly from XML returned by the (attacker-controllable) SVN server/repository and is never checked for `..` path-traversal segments or absolute paths before being joined into a filesystem path, mirroring the CVE-2017-6200 root cause of trusting an unfiltered file list to build file paths for an archive.

### Finding Description
Attacker input: a module author controls the content of an SVN repository (`go get`/`go mod download` fetches from arbitrary SVN remotes). Go entry point: `codeRepo.Zip` → `codehost.svnReadZip` ( [1](#0-0) ). The function issues `svn list --xml --recursive` and unmarshals entries into `listEntry{Name string}` without any validation [2](#0-1) . It then loops over these attacker-controlled names and does:
```
zf, err := zw.Create(path.Join(basePath, e.Name))
f, err := os.Open(filepath.Join(exportDir, e.Name))
``` [3](#0-2) 
No call to `module.CheckFilePath`, `filepath.IsLocal`, or any `..`/absolute-path rejection is performed on `e.Name` before it is joined into `exportDir` — my search for `CheckFilePath|CheckImportPath|IsLocal|\.\./` under `src/cmd/go/internal/modfetch/` found no matches in `svn.go` (only test files and an unrelated hit in `codehost.go`). If `e.Name` contains sequences such as `../../../../etc/passwd`, `filepath.Join(exportDir, e.Name)` can escape `exportDir` and read arbitrary files reachable from the process's working directory on the machine running `go get`. This is the same bug class as CVE-2017-6200: a directory/file listing is trusted verbatim as a set of path components used to construct filesystem paths, without filtering `..` or path separators, enabling reads outside the intended root.

### Impact Explanation
If reachable, this allows a malicious SVN module source to make `go get`/`go mod download` read files from outside the intended export directory and package their contents into the generated module zip — a file-disclosure primitive triggered by an ordinary `go get` of a published (malicious) module, i.e., exactly the "published module/archive/source consumed by a normal victim workflow" premise permitted by the rules. This would be a genuine security bug (material disclosure of local files into a downloadable/importable zip) and would likely be handled on Go's PRIVATE/URGENT security track if proven exploitable, since it affects the module-fetching trust boundary.

### Likelihood Explanation
The victim workflow is any `go get`, `go mod download`, or build that resolves a dependency hosted in an SVN repository (SVN support in `cmd/go` is legacy/rare but still present and reachable via `go.mod` with `svn` VCS metadata or `GOVCS`). The attacker only needs to control the SVN server/repository content returned to `svn list`, which is the normal capability of "publishing a module" — not a "malicious server takeover" of Go's own infrastructure. However, `svn list --xml` output for `name` attributes is normally derived from actual file names within the repository as stored by the SVN server software; whether SVN itself permits creating a versioned path entry containing literal `../` segments (as opposed to encoding them) is uncertain from static review alone — this would need to be verified against real `svn`/`svnadmin` behavior, since SVN's own path model may reject or normalize such names before they can ever appear in `svn list` output.

### Recommendation
Validate every `e.Name` from the `svn list --xml` output with `filepath.IsLocal` (or an equivalent check rejecting empty, absolute, or `..`-containing components) before using it in `filepath.Join(exportDir, e.Name)` or `zw.Create(path.Join(basePath, e.Name))`, mirroring the `ErrInsecurePath`/`filepath.IsLocal` checks already used in `archive/zip.Reader.init` and `archive/tar.Reader.Next` [4](#0-3) [5](#0-4) .

### Proof of Concept
Because reproducing this requires an actual `svn` binary and a crafted repository whose `svn list --xml` output contains a `name` attribute with `../` sequences (unverified whether real SVN permits creating such paths), a full runnable Go test cannot be constructed from static analysis alone. A minimal illustrative unit test (isolating just the vulnerable path-construction logic, independent of invoking real `svn`) would be:

```go
func TestSvnReadZipPathTraversal(t *testing.T) {
    exportDir := t.TempDir()
    // Simulate a "svn list" entry with a path-traversal name.
    maliciousName := "../../../../etc/passwd"
    // This mirrors the vulnerable line in svnReadZip:
    p := filepath.Join(exportDir, maliciousName)
    if filepath.IsLocal(maliciousName) {
        t.Fatalf("expected non-local name to be rejected")
    }
    // svnReadZip currently has no such check, so p resolves outside exportDir:
    if !strings.HasPrefix(p, exportDir) {
        t.Logf("path escapes exportDir as in svnReadZip: %s", p)
    }
}
```
This demonstrates the missing validation; a true end-to-end exploit would require confirming that a real SVN server can be made to report such a `name` via `svn list --xml`, which I was unable to verify with the tools available.

### Citations

**File:** src/cmd/go/internal/modfetch/codehost/svn.go (L47-47)
```go
func svnReadZip(ctx context.Context, dst io.Writer, workDir, rev, subdir, remote string) (err error) {
```

**File:** src/cmd/go/internal/modfetch/codehost/svn.go (L89-99)
```go
	type listEntry struct {
		Kind string `xml:"kind,attr"`
		Name string `xml:"name"`
		Size int64  `xml:"size"`
	}
	var list struct {
		Entries []listEntry `xml:"entry"`
	}
	if err := xml.Unmarshal(out, &list); err != nil {
		return vcsErrorf("unexpected response from svn list --xml: %v\n%s", err, out)
	}
```

**File:** src/cmd/go/internal/modfetch/codehost/svn.go (L144-149)
```go
		zf, err := zw.Create(path.Join(basePath, e.Name))
		if err != nil {
			return err
		}

		f, err := os.Open(filepath.Join(exportDir, e.Name))
```

**File:** src/archive/zip/reader.go (L162-175)
```go
	if zipinsecurepath.Value() == "0" {
		for _, f := range r.File {
			if f.Name == "" {
				// Zip permits an empty file name field.
				continue
			}
			// The zip specification states that names must use forward slashes,
			// so consider any backslashes in the name insecure.
			if !filepath.IsLocal(f.Name) || strings.Contains(f.Name, `\`) {
				zipinsecurepath.IncNonDefault()
				return ErrInsecurePath
			}
		}
	}
```

**File:** src/archive/tar/reader.go (L56-69)
```go
func (tr *Reader) Next() (*Header, error) {
	if tr.err != nil {
		return nil, tr.err
	}
	hdr, err := tr.next()
	tr.err = err
	if err == nil && !filepath.IsLocal(hdr.Name) {
		if tarinsecurepath.Value() == "0" {
			tarinsecurepath.IncNonDefault()
			err = ErrInsecurePath
		}
	}
	return hdr, err
}
```
