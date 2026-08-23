### Title
Unbounded accumulation of reference updates and push options in proc-receive handler enables memory-exhaustion DoS - ([File: internal/gitaly/hook/procreceive_handler.go])

### Summary
`NewProcReceiveHandler` parses the `git-receive-pack(1)` proc-receive protocol stream (reference updates and push options) sent over the hook's stdin and accumulates every entry into unbounded in-memory slices, with no limit on the number of entries or their aggregate size, before any processing occurs.

### Finding Description
When transactions are enabled, `RegisterProcReceiveHook` in `internal/gitaly/hook/receivepack/receive_pack.go` registers a goroutine that waits on the proc-receive hook handler created by `NewProcReceiveHandler`. [1](#0-0) 

`NewProcReceiveHandler` reads pkt-lines from the hook stdin and, for each ref update line, appends a parsed `ReferenceUpdate` to the `updates` slice with no bound on the loop iterations: [2](#0-1) 

It then similarly reads an unbounded number of push options into the `pushOptions` slice: [3](#0-2) 

Each individual pkt-line packet is capped at `MaxPktSize` (65520 bytes) by the pktline scanner, [4](#0-3)  but there is no cap on the *number* of packets/lines processed in either loop, nor on the total accumulated size of `updates` or `pushOptions`. This data originates from `git-receive-pack(1)`'s execute-commands step, which is driven directly by the ref updates and `-o`/push-option values supplied by an ordinary client performing `git push`. A client can push a very large number of small reference updates (deletions/creations of many refs require minimal pack data) or repeat push options extensively, causing the handler to buffer an effectively attacker-controlled amount of data in memory in a single goroutine per push before any reference update or push-option is validated or acted upon.

### Impact Explanation
An unauthenticated-in-effect but ordinary authorized push client can trigger unbounded memory allocation on the Gitaly node handling `PostReceivePack`/`SSHReceivePack` when transactions (and the proc-receive hook) are enabled, potentially exhausting server memory and degrading or crashing the Gitaly process, affecting all repositories served by that node. This aligns with the reported bug class of insufficient size/limit validation on RPC-relayed input leading to resource exhaustion.

### Likelihood Explanation
Any user capable of pushing to a repository (a routine, low-privilege operation) can supply arbitrarily many small reference updates or push options in a single push; no special access or malicious peer/token compromise is required, making this readily reachable through the ordinary push code path once the feature (proc-receive with transactions) is active.

### Recommendation
Enforce explicit upper bounds while parsing the proc-receive stream in `NewProcReceiveHandler`:
- Cap the maximum number of reference updates and push options accepted per push (returning a clear error / rejecting the push once exceeded).
- Cap the cumulative byte size consumed by these loops.
- Consider streaming/chunked processing instead of fully buffering all updates/push options before validation, and add the same protections to any other RPC-handler parsing code that accumulates repeated client-supplied fields without limit.

### Proof of Concept
Not independently reproduced in this environment (read-only analysis); the vulnerable pattern is the unbounded `for scanner.Scan() { ... append(...) }` loops shown above with no iteration/size limits, reachable by crafting a push with a very large number of ref updates or `git push -o` push options.

### Citations

**File:** internal/gitaly/hook/receivepack/receive_pack.go (L44-60)
```go
	registry := hookManager.ProcReceiveRegistry()
	handlerCh, cleanup, err := registry.RegisterWaiter(transactionID)
	if err != nil {
		return nil, fmt.Errorf("registering waiter: %w", err)
	}

	go func() {
		select {
		case <-ctx.Done():
		case <-receiveDoneCh:
		case handler := <-handlerCh:
			if err := procReceiveHook(ctx, logger, cfg, req, repo, hookManager, tx, handler); err != nil {
				handlerErrCh <- err
			}
		}
		close(handlerErrCh)
	}()
```

**File:** internal/gitaly/hook/procreceive_handler.go (L80-99)
```go
	var updates []ReferenceUpdate
	for scanner.Scan() {
		line := scanner.Bytes()

		// When all reference updates are transmitted, we expect a flush.
		if pktline.IsFlush(line) {
			break
		}

		data, err := pktline.Payload(line)
		if err != nil {
			return nil, nil, fmt.Errorf("receiving reference update: %w", err)
		}

		update, err := parseRefUpdate(data)
		if err != nil {
			return nil, nil, fmt.Errorf("parse reference update: %w", err)
		}
		updates = append(updates, update)
	}
```

**File:** internal/gitaly/hook/procreceive_handler.go (L101-118)
```go
	var pushOptions []string
	if featureRequests.pushOptions {
		for scanner.Scan() {
			line := scanner.Bytes()

			// When all push options are transmitted, we expect a flush.
			if pktline.IsFlush(line) {
				break
			}

			pushOption, err := pktline.Payload(line)
			if err != nil {
				return nil, nil, fmt.Errorf("getting push option payload: %w", err)
			}

			pushOptions = append(pushOptions, string(pushOption))
		}
	}
```

**File:** internal/git/pktline/pktline.go (L15-24)
```go
const (
	// MaxSidebandData is the maximum number of bytes that fits into one Git
	// pktline side-band-64k packet.
	MaxSidebandData = MaxPktSize - 5

	// MaxPktSize is the maximum size of content of a Git pktline side-band-64k
	// packet, including size of length and band number
	// https://gitlab.com/gitlab-org/git/-/blob/v2.30.0/pkt-line.h#L216
	MaxPktSize = 65520
)
```
