Based on my investigation, I found a concrete analog: an unchecked `int32` addition in `internal/gitaly/service/commit/find_commits.go` that combines two independent, fully user-controlled RPC fields (`Limit` and `Offset`, both declared as `int32` in `proto/commit.proto`) and feeds the result directly into a `git log` command-line argument.

### Title
Integer overflow in FindCommits offset/limit arithmetic can inject a negative `--max-count` value into git-log(1) - ([File: internal/gitaly/service/commit/find_commits.go])

### Summary
`getLogCommandSubCmd` computes `limit := req.GetLimit(); limit += req.GetOffset()` when `calculateOffsetManually` is true, then formats it unchecked into `--max-count=%d`. Both `Limit` and `Offset` are attacker-controlled `int32` protobuf fields with no arithmetic-overflow protection, mirroring the reported bug class ("lack of overflow protection... for unsigned/fixed-width integers... can lead to incorrect values used downstream").

### Finding Description
`FindCommits` builds its underlying `git log` invocation in `getLogCommandSubCmd`: [1](#0-0) 
`calculateOffsetManually` returns true whenever `Follow` is set and `Offset > 0`: [2](#0-1) 
`Limit` and `Offset` are ordinary `int32` fields on `FindCommitsRequest`, fully controlled by any caller of the RPC with no upper-bound validation performed before this addition (only a general request validation path exists elsewhere in the service, but no check bounds `Limit + Offset`). When `Limit` is close to `math.MaxInt32` and `Offset` is also large and positive, `limit += req.GetOffset()` overflows the 32-bit signed integer and wraps to a negative value. This value is then formatted directly into the git command line as `--max-count=%d`, i.e., `--max-count=-1873...` or similar, without any sanitation of the computed (post-addition) value — only the raw input fields are checked for negativity by validators elsewhere, not the derived sum.

This is architecturally identical to the reported class: absent overflow protection on fixed-width integer arithmetic that is subsequently used to construct a critical operation (a git subprocess argument, analogous to "calculating coin amounts" in the original report — here it is "calculating command line limits").

### Impact Explanation
A negative `--max-count` argument changes how `git-log(1)` behaves compared to the intended positive limit semantics that Gitaly's calling code (GitLab Rails) expects, subverting the pagination/limit contract of the RPC. Since `git log --max-count=<negative>` is not rejected by git in all versions and its behavior differs from the intended finite/capped output, an unprivileged caller can use crafted `Limit`/`Offset` values to bypass the intended bound on the number of commits streamed back, undermining the RPC's resource-limiting contract (a form of limit-check bypass on a git command construction path reachable directly from an ordinary user-issued RPC field).

### Likelihood Explanation
High reachability: `FindCommits` is a standard, unprivileged-facing RPC and both `Limit` and `Offset` are simple scalar fields with no server-side range validation preventing the sum from overflowing `int32`. Any client that can call `FindCommits` (i.e., any authenticated Gitaly client, which in GitLab's trust model can be an ordinary user acting through GitLab Rails) can trigger this by choosing near-`MaxInt32` values for both fields together with `Follow=true`.

### Recommendation
Validate `Limit` and `Offset` bounds (or use 64-bit/saturating arithmetic) before computing `limit := req.GetLimit() + req.GetOffset()` in `getLogCommandSubCmd`, and reject or clamp values that would overflow `int32` before formatting them into the `--max-count` git-log argument.

### Proof of Concept
Conceptual reproduction (not executed):
1. Call `FindCommits` with `Follow: true`, `Limit: 2147483000`, `Offset: 2000000000`.
2. `calculateOffsetManually` returns true (`Follow && Offset > 0`).
3. `limit := req.GetLimit()` (2147483000) `+= req.GetOffset()` (2000000000) overflows `int32`, producing a large negative number.
4. `subCmd.Flags` receives `gitcmd.Flag{Name: fmt.Sprintf("--max-count=%d", limit)}` with the negative value, which is passed to `git log`, altering the intended limiting behavior. [3](#0-2) 

**Note on confidence**: I could not fully verify at what layer (if any) GitLab Rails or an upstream gRPC interceptor might already clamp `Limit`/`Offset` to safe ranges before reaching Gitaly, nor could I confirm git's exact behavior for negative `--max-count` across all supported git versions within the scope of this index-based search — a background Devin session with full repo/tool access would be needed to trace the complete validation chain and confirm exploitability end-to-end.

### Citations

**File:** internal/gitaly/service/commit/find_commits.go (L114-116)
```go
func calculateOffsetManually(req *gitalypb.FindCommitsRequest) bool {
	return req.GetFollow() && req.GetOffset() > 0
}
```

**File:** internal/gitaly/service/commit/find_commits.go (L257-266)
```go
	//  We will perform the offset in Go because --follow doesn't play well with --skip.
	//  See: https://gitlab.com/gitlab-org/gitlab-ce/issues/3574#note_3040520
	if req.GetOffset() > 0 && !calculateOffsetManually(req) {
		subCmd.Flags = append(subCmd.Flags, gitcmd.Flag{Name: fmt.Sprintf("--skip=%d", req.GetOffset())})
	}
	limit := req.GetLimit()
	if calculateOffsetManually(req) {
		limit += req.GetOffset()
	}
	subCmd.Flags = append(subCmd.Flags, gitcmd.Flag{Name: fmt.Sprintf("--max-count=%d", limit)})
```
