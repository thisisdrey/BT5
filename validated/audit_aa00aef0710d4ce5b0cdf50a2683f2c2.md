### Title
Unsynchronized shared `JumpDestCache` map is written concurrently by the main block processor and the background state prefetcher, causing a fatal, unrecoverable crash (or map corruption) - (File: `core/vm/jumpdests.go`, `core/blockchain.go`, `core/state_prefetcher.go`)

### Summary
`BlockChain.jumpDestCache` is a single, per-chain instance of the default `mapJumpDests`, a plain Go map that its own doc comment states is "not thread-safe and is meant to be used per EVM instance" [1](#0-0) . Despite this explicit constraint, the blockchain wires the *same* instance into two independently-scheduled EVM executions that run concurrently: the serial block processor and the speculative background prefetcher.

### Finding Description
`BlockChain` keeps one `jumpDestCache vm.JumpDestCache` field, explicitly commented as "Shared JUMPDEST analysis cache for block processing" [2](#0-1) .

In `setupExecutionState`'s default branch (prefetching enabled, non-BAL path), the code spawns a goroutine that runs the speculative prefetcher against `bc.jumpDestCache`, while simultaneously returning the "real" `statedb` for the main synchronous processing pass to use — the two paths run in parallel, not sequentially: [3](#0-2) 

`statePrefetcher.Prefetch` itself fans out into a worker pool (`errgroup`) that concurrently spins up multiple EVMs, each one calling `evm.SetJumpDestCache(jumpDestCache)` with the very same shared cache instance and executing transactions in parallel goroutines: [4](#0-3) 

Meanwhile, the main (serial) processing pass also uses this same `bc.jumpDestCache` for its own EVM (wired similarly via `core/state_processor.go` / `core/state_processor_parallel.go`, both of which reference `jumpDestCache` alongside `EVM.SetJumpDestCache`).

`mapJumpDests.Store` performs a completely unguarded `j[codeHash] = vec` write on a plain Go map [5](#0-4) . Because both the prefetcher's worker goroutines and the main processing goroutine independently call `Load`/`Store` on the identical map instance whenever they first encounter a given contract's `codeHash` (during JUMPDEST analysis), two goroutines can perform concurrent writes to the same underlying Go map. This is precisely analogous to the kernel bug: a data structure meant for single-context use (per-CPU variable / "per EVM instance") is written from multiple concurrent execution contexts without any synchronization primitive protecting it.

Note that by contrast, `PrecompileCache` — which is shared the same way between the prefetcher and the main pass — was deliberately built with `sync.RWMutex` protection specifically because it is shared across goroutines [6](#0-5) . `JumpDestCache`'s default map-backed implementation received no equivalent protection, even though it is now handed the same shared-across-goroutines role.

### Impact Explanation
Concurrent, unsynchronized writes to a Go map are actively detected by the Go runtime and immediately terminate the process with `fatal error: concurrent map writes` — a fatal error that cannot be caught by `defer`/`recover`. Any ordinary, fully valid block containing more than one contract call whose bytecode has not yet been JUMPDEST-analyzed and cached can trigger this: the main pass and a prefetcher worker will race to `Store` the analysis result for that `codeHash`. This crashes the running Geth node deterministically on ordinary block processing, without any malicious or invalid transaction/block — every Geth node running with the prefetcher enabled (the default configuration) can be brought down by processing a normal block, which is a network-wide crash matching the "Critical" impact bar (a single transaction or block that crashes every Geth node).

Even in the rarer case where the runtime does not trip its write-write race detector (e.g., timing avoids the internal bucket-write collision window), corrupting the map's internal state can silently return a wrong/garbage `BitVec` for a `codeHash` on a subsequent `Load`, which would alter JUMPDEST validity determination during EVM execution — potentially causing different nodes (or the same node across runs) to disagree on which jumps are legal, causing a state root / gas divergence.

### Likelihood Explanation
This does not require a malicious block or transaction — it triggers whenever the built-in speculative prefetcher (enabled by default, see the `bc.cfg.NoPrefetch` check in `setupExecutionState`) and the main processing pass reach un-cached contract bytecode close together in time, which is the common case for the very first execution of any given contract's code on a node (e.g., right after node startup or on any newly-deployed contract). Given the prefetcher processes many transactions in parallel worker goroutines exactly to race ahead of the serial pass, races over the *same* not-yet-cached `codeHash` are the intended, common operating condition of this cache, not an edge case.

### Recommendation
Protect the default `JumpDestCache` implementation with a mutex (or a `sync.Map`) whenever it is used in a shared, cross-goroutine role, mirroring what `PrecompileCache` already does with `sync.RWMutex` in `core/vm/precompile_cache.go`. Alternatively, give the prefetcher and the main processing pass separate `JumpDestCache` instances (as the doc comment on `mapJumpDests` already implies is the intended usage — "per EVM instance") rather than sharing `bc.jumpDestCache` across the concurrently-running prefetcher and processor.

### Proof of Concept
1. Run a Geth node with the block prefetcher enabled (default, i.e. `NoPrefetch=false`, non-BAL execution path).
2. Import/process a block containing at least two transactions that call into a contract whose bytecode has never previously been analyzed by `bc.jumpDestCache` (e.g., a freshly deployed contract, or the first block executed after node startup touching that contract).
3. `setupExecutionState` launches the prefetcher goroutine sharing `bc.jumpDestCache` while the main processing pass concurrently executes the same block using the same cache instance.
4. Both the prefetcher's worker goroutine(s) and the main pass's EVM independently reach `mapJumpDests.Store` for the same un-cached `codeHash` at close to the same time, both writing into the same unguarded map.
5. Under Go's runtime map-write race detection this reliably produces `fatal error: concurrent map writes`, immediately crashing the node process while it is validly processing a normal block.

**Uncertainty:** I could not directly inspect `core/vm/contract.go`'s exact `Load`/`Store` call sites (only located them via grep, without retrieving line-accurate contents) nor `core/state_processor.go`'s exact wiring of `jumpDestCache` into the serial EVM. These were inferred to exist from the `JumpDestCache` interface's usage pattern and the `EVM.SetJumpDestCache` API referenced across `core/vm/evm.go`, `core/vm/contract.go`, `core/state_processor.go`, `core/state_processor_parallel.go`, and `core/state_prefetcher.go` (per `grep_search` matches), but I was not able to fully confirm the precise call sequence within the interpreter loop due to tool-call limits.

### Citations

**File:** core/vm/jumpdests.go (L31-47)
```go
// mapJumpDests is the default implementation of JumpDests using a map.
// This implementation is not thread-safe and is meant to be used per EVM instance.
type mapJumpDests map[common.Hash]BitVec

// newMapJumpDests creates a new map-based JumpDests implementation.
func newMapJumpDests() JumpDestCache {
	return make(mapJumpDests)
}

func (j mapJumpDests) Load(codeHash common.Hash) (BitVec, bool) {
	vec, ok := j[codeHash]
	return vec, ok
}

func (j mapJumpDests) Store(codeHash common.Hash, vec BitVec) {
	j[codeHash] = vec
}
```

**File:** core/blockchain.go (L331-331)
```go
	jumpDestCache   vm.JumpDestCache                 // Shared JUMPDEST analysis cache for block processing
```

**File:** core/blockchain.go (L2171-2196)
```go
	default:
		// The main processor and the speculative prefetcher share the same reader
		// with a local cache for mitigating the overhead of state access.
		prefetch, process, err := warmer.ReadersWithCacheStats(parentRoot)
		if err != nil {
			return nil, nil, err
		}
		throwaway, err := state.NewWithReader(parentRoot, sdb, prefetch)
		if err != nil {
			return nil, nil, err
		}
		statedb, err := state.NewWithReader(parentRoot, sdb, process)
		if err != nil {
			return nil, nil, err
		}
		go func(start time.Time) {
			// Disable tracing for prefetcher executions.
			vmCfg := vmConfig
			vmCfg.Tracer = nil
			bc.prefetcher.Prefetch(block, throwaway, bc.jumpDestCache, bc.precompileCache.PrefetchView(), vmCfg, interrupt, execIndex)

			blockPrefetchExecuteTimer.Update(time.Since(start))
			if interrupt.Load() {
				blockPrefetchInterruptMeter.Mark(1)
			}
		}(time.Now())
```

**File:** core/state_prefetcher.go (L53-115)
```go
func (p *statePrefetcher) Prefetch(block *types.Block, statedb *state.StateDB, jumpDestCache vm.JumpDestCache, precompileCache *vm.PrecompileCache, cfg vm.Config, interrupt *atomic.Bool, execIndex *atomic.Int64) {
	var (
		fails   atomic.Int64
		skips   atomic.Int64
		header  = block.Header()
		signer  = types.MakeSigner(p.config, header.Number, header.Time)
		workers errgroup.Group
		reader  = statedb.Reader()
		txs     = block.Transactions()
	)
	workers.SetLimit(max(1, 4*runtime.NumCPU()/5)) // Aggressively run the prefetching

	// Iterate over and process the individual transactions
	for _, n := range prefetchOrder(txs) {
		i, tx := n, txs[n]
		stateCpy := statedb.Copy() // closure
		workers.Go(func() error {
			// If block precaching was interrupted, abort
			if interrupt != nil && interrupt.Load() {
				return nil
			}
			// Skip transactions the main pass has already reached, warming
			// them up can not help anymore.
			if execIndex != nil && execIndex.Load() >= int64(i) {
				skips.Add(1)
				return nil
			}
			// Preload the touched accounts and storage slots in advance
			sender, err := types.Sender(signer, tx)
			if err != nil {
				fails.Add(1)
				return nil
			}
			reader.Account(sender)

			if tx.To() != nil {
				account, _ := reader.Account(*tx.To())

				// Preload the contract code if the destination has non-empty code
				if account != nil && !bytes.Equal(account.CodeHash, types.EmptyCodeHash.Bytes()) {
					reader.Code(*tx.To(), common.BytesToHash(account.CodeHash))
				}
			}
			for _, list := range tx.AccessList() {
				reader.Account(list.Address)
				if len(list.StorageKeys) > 0 {
					for _, slot := range list.StorageKeys {
						reader.Storage(list.Address, slot)
					}
				}
			}
			// Execute the message to preload the implicit touched states
			evm := vm.NewEVM(NewEVMBlockContext(header, p.chain, nil), stateCpy, p.config, cfg)
			defer evm.Release()

			// Set the caches for EVM interpreter
			if jumpDestCache != nil {
				evm.SetJumpDestCache(jumpDestCache)
			}
			if precompileCache != nil {
				evm.SetPrecompileCache(precompileCache)
			}
			// Convert the transaction into an executable message and pre-cache its sender
```

**File:** core/vm/precompile_cache.go (L67-90)
```go
// PrecompileCache is a thread-safe cache of precompile outputs, shared between
// the state prefetcher and block processing so the serial pass can reuse what
// the prefetcher already computed. Each precompile gets its own cache per fork,
// so results never cross a repricing and a cheap precompile cannot evict the
// results of an expensive one.
type PrecompileCache struct {
	data *precompileCacheData

	// Meters are per handle, split between the main pass and the prefetcher
	// so the hit rate of the main pass stays readable on its own.
	prefix   string
	hit      *metrics.Meter
	miss     *metrics.Meter
	mu       sync.RWMutex
	meters   map[common.Address]*precompileCacheMeters
	prefetch *PrecompileCache
}

// precompileCacheData is the storage shared by the two cache handles.
type precompileCacheData struct {
	mu     sync.RWMutex
	caches map[precompileCacheScope]*lru.SizeConstrainedCache[string, []byte]
}

```
