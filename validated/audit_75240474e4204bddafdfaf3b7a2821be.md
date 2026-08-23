### Title
Unbounded, Unsanitized User-Controlled PCRE Regex in `SearchFilesByContent` Enables ReDoS / CPU Exhaustion - (File: internal/gitaly/service/repository/search_files.go)

### Summary
The `SearchFilesByContent` RPC handler passes the client-supplied `Query` field directly and unbounded into `git grep --perl-regexp -e <query>` without any length cap, complexity check, or metacharacter sanitization, unlike its sibling RPC `SearchFilesByName`, which explicitly caps and validates its regex `Filter` field.

### Finding Description
`SearchFilesByContent` builds a `git grep` invocation with `--perl-regexp` and feeds `req.GetQuery()` straight into the `-e` value flag: [1](#0-0) 

`validateSearchFilesRequest`, the only validation applied to this field, merely checks that the repository, `Ref`, and `Query` are non-empty — it performs no length limit and no regex-safety check: [2](#0-1) 

This is asymmetric with the neighboring `SearchFilesByName` RPC in the same file, which treats its regex-bearing `Filter` field as untrusted: it enforces a hard 1000-byte cap (`searchFilesFilterMaxLength`) and compiles it with Go's RE2-based `regexp` package (which has no catastrophic-backtracking behavior) before use, rejecting anything that fails to compile: [3](#0-2) 

The proto documentation for `SearchFilesByContentRequest.query` itself confirms the query is fed straight to a Perl-compatible regex engine with no stated size limit: [4](#0-3) 

PCRE-style engines (unlike RE2) are vulnerable to catastrophic backtracking on crafted patterns (e.g. nested quantifiers like `(a+)+$`), and here the pattern is both attacker-controlled and unbounded in length, then executed as a subprocess (`git grep`) against arbitrary repository content that the attacker also controls in scope (they typically have push/write access to at least one repository they can query against).

### Impact Explanation
An authenticated caller with read access to any repository accessible via Gitaly can submit a `SearchFilesByContentRequest` with a pathological Perl-compatible regex `Query` against a ref they control. The resulting `git grep --perl-regexp` subprocess can be driven into exponential backtracking, consuming CPU on the Gitaly node for the duration of the request. Because Gitaly is typically multi-tenant (many repositories per storage node), sustained or repeated invocation of this handler can degrade or exhaust CPU resources shared by other repositories/tenants on the same node — a DoS of the RPC handler and potentially the node it runs on. This mirrors the ReDoS/resource-exhaustion impact flagged in the referenced report, but the root cause and blast radius here are Gitaly-specific: unmoderated regex complexity fed into a real subprocess without the length/RE2 safeguards Gitaly already applies to its sibling RPC.

### Likelihood Explanation
Likelihood is moderate: exploitation requires only a valid, authenticated RPC call to `SearchFilesByContent` with attacker-chosen `Query` content and a reachable `Ref` — no privileged role, leaked token, or malicious peer is needed, satisfying the "ordinary user's crafted RPC field" bar. The main mitigating factor is that Gitaly's overall RPC context deadline and any cluster-level load-shedding (as described in the load-management docs) may eventually cancel or limit a single very-long-running request, but this is a coarse, node-wide mechanism, not a per-field regex safety check, and does not prevent CPU burn during the window before cancellation, nor repeated/parallel abuse across many requests.

### Recommendation
- Apply the same protections already used for `SearchFilesByName`'s `Filter` field to `SearchFilesByContent`'s `Query`: enforce a maximum length (e.g. reuse or introduce a constant analogous to `searchFilesFilterMaxLength`).
- Where feasible, pre-validate/compile the pattern with a backtracking-safe engine (Go's RE2-based `regexp`) to reject obviously pathological constructs before invoking `git grep --perl-regexp`, or drop `--perl-regexp` in favor of a non-backtracking mode when strict PCRE features aren't required.
- Consider applying a dedicated CPU/time budget or `git grep` execution timeout scoped to this specific RPC, independent of the general RPC deadline, so a single pathological query cannot consume disproportionate CPU before being reaped.

### Proof of Concept
```
SearchFilesByContentRequest{
  Repository: <repo the caller has read access to>,
  Ref:        []byte("<any valid ref>"),
  Query:      "(a+)+$",   // or another PCRE catastrophic-backtracking pattern,
                          // no length limit enforced, unlike SearchFilesByName.Filter
}
```
Sent repeatedly/concurrently against a repository with content that triggers extended backtracking (e.g. long strings of `a` characters without a terminating match), this drives `git grep --perl-regexp` subprocesses into exponential-time evaluation, consuming CPU on the serving Gitaly node for each in-flight request — as validated conceptually by the handler code path shown in [1](#0-0)  versus the safeguards present only in the `SearchFilesByName` path at [3](#0-2) .

### Citations

**File:** internal/gitaly/service/repository/search_files.go (L41-56)
```go
	cmd, err := repo.Exec(ctx, gitcmd.Command{
		Name: "grep",
		Flags: []gitcmd.Option{
			gitcmd.Flag{Name: "--ignore-case"},
			gitcmd.Flag{Name: "-I"},
			gitcmd.Flag{Name: "--line-number"},
			gitcmd.Flag{Name: "--null"},
			gitcmd.ValueFlag{Name: "--before-context", Value: surroundContext},
			gitcmd.ValueFlag{Name: "--after-context", Value: surroundContext},
			gitcmd.Flag{Name: "--perl-regexp"},
			gitcmd.ValueFlag{Name: "-e", Value: req.GetQuery()},
		},
		Args: []string{
			string(req.GetRef()),
		},
	}, gitcmd.WithSetupStdout())
```

**File:** internal/gitaly/service/repository/search_files.go (L118-128)
```go
	var filter *regexp.Regexp
	if req.GetFilter() != "" {
		if len(req.GetFilter()) > searchFilesFilterMaxLength {
			return structerr.NewInvalidArgument("filter exceeds maximum length")
		}
		var err error
		filter, err = regexp.Compile(req.GetFilter())
		if err != nil {
			return structerr.NewInvalidArgument("filter did not compile: %w", err)
		}
	}
```

**File:** internal/gitaly/service/repository/search_files.go (L174-192)
```go
func validateSearchFilesRequest(ctx context.Context, locator storage.Locator, req searchFilesRequest) error {
	if err := locator.ValidateRepository(ctx, req.GetRepository()); err != nil {
		return err
	}

	if len(req.GetQuery()) == 0 {
		return errors.New("no query given")
	}

	if len(req.GetRef()) == 0 {
		return errors.New("no ref given")
	}

	if bytes.HasPrefix(req.GetRef(), []byte("-")) {
		return errors.New("invalid ref argument")
	}

	return nil
}
```

**File:** proto/repository.proto (L1206-1219)
```text
// SearchFilesByContentRequest is a request for the SearchFilesByContent RPC.
message SearchFilesByContentRequest {
  // repository is the repo to search. The storage_name and relative_path attributes must be provided.
  Repository repository = 1 [(target_repository)=true];
  // query is the grep pattern to use. Queries are case-insensitive and are compatible
  // with Perl regexp syntax.
  string query = 2;
  // ref is the reference to limit the search scope by, for example a commit or
  // branch name.
  bytes ref = 3;
  // chunked_response is deprecated as it was never used.
  reserved 4;
  reserved "chunked_response";
}
```
