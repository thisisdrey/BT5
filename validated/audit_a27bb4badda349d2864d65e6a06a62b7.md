### Title
`finaliseAmsterdam` self-destruct-with-balance path fails to clear nonce/code/storage, contradicting EIP-8264 semantics - (File: `core/state/statedb.go`)

### Summary
In `StateDB.finaliseAmsterdam`, when a self-destructed account retains a non-zero balance, the code is supposed to preserve only the balance while resetting nonce to 0, clearing code, and clearing all storage per EIP-8264. Instead, the replacement state object is built from the pre-block `origin` account snapshot, silently carrying over the old nonce, code hash, and storage root instead of clearing them.

### Finding Description
The relevant logic: [1](#0-0) 

```go
case obj.selfDestructed:
    // EIP-8264: accounts marked for self-destruction, instead of
    // being deleted, are modified as follows:
    // - nonce is reset to 0,
    // - balance is unchanged,
    // - code is cleared,
    // - all storage is cleared
    if !obj.Balance().IsZero() {
        o := newObject(s, obj.address, obj.origin)
        o.setBalance(new(uint256.Int).Set(obj.Balance()))
        s.setStateObject(o)
        s.markUpdate(addr)
    } else {
        ...
```

`newObject` is defined as: [2](#0-1) 

`newObject(db, address, acct)` sets `origin = acct` and `data = *acct`. Because `acct` here is `obj.origin` (the account's pre-block snapshot, which still has the live/valid `Nonce`, `CodeHash`, and `Root` from before self-destruction), the freshly created object `o` inherits that **old** nonce, code hash, and storage root wholesale. The subsequent `o.setBalance(...)` only overwrites the balance field — nonce is *not* reset to 0, `CodeHash` is *not* cleared to `types.EmptyCodeHash`, and `Root` is *not* cleared to `types.EmptyRootHash`, directly contradicting the comment describing the intended EIP-8264 behavior immediately above it.

Additionally, because a brand-new `stateObject` is created instead of calling `obj.finalise()`, any dirty/pending storage mutations accumulated by `obj` earlier in the block (e.g., SSTOREs performed by prior transactions or within the same transaction before the SELFDESTRUCT) are discarded outright — `o` starts with fresh empty `originStorage`/`pendingStorage`/`dirtyStorage`/`uncommittedStorage` maps and an account `data` snapshot rolled all the way back to the state at block start.

Net effect: an account that self-destructs but keeps a non-zero balance is persisted with its pre-block code, nonce, and storage root — i.e., functionally unaffected — rather than being reduced to a balance-only account as EIP-8264 mandates. When queried on the same/subsequent block, `GetCommittedState`/`Code`/`CodeHash`/`Nonce` for that address will resolve to the stale pre-destruct values because the object still carries the old `CodeHash`/`Root`, and `s.stateObjectsDestruct` is never populated for this address in this branch (only the zero-balance branch adds to it), so no storage-wipe logic ever runs for it either.

### Impact Explanation
This produces a computed `stateRoot`/account state that diverges from what the EIP-8264 rule requires (nonce=0, code cleared, storage cleared, balance preserved). Concretely:
- The account's code remains live and callable in perpetuity despite having self-destructed, which is functionally equivalent to code persisting on an account that was supposed to be "cleared" — the exact bug-class analog of the Solidity report (a `delete`/clear operation on a composite value that silently fails to remove the underlying data, leaving stale state reachable).
- Storage mutations made earlier in the same block by the destructing account are silently reverted, and the account's `Root`/`CodeHash`/`Nonce` remain pinned to pre-block values instead of the EIP-8264-mandated cleared values, producing a state root that differs from the one the specification dictates for the same block.

This falls under the "High" impact bucket in the rules ("persistence that no longer matches execution after a valid block/reorg" / gas-or-state divergence on a fork-specific path), since every node applying this exact logic on the Amsterdam fork would compute the same (wrong) state independently, but that computed state deviates from the EIP-8264 text and from what an implementation with correct clearing semantics would produce.

### Likelihood Explanation
Triggering requires only a straightforward contract that (a) performs some storage writes, then (b) self-destructs to itself (or with a balance that isn't fully drained) on a chain running under the Amsterdam ruleset (`rules.IsAmsterdam`) where the SELFDESTRUCT-burn removal (EIP-8246) leaves a non-zero balance. This is a deterministic code path, not a race or timing issue, and executes on the ordinary transaction-processing flow (`Finalise` → `finaliseAmsterdam`), not on any RPC-only or adversarial-peer surface.

### Recommendation
In the `obj.selfDestructed && !obj.Balance().IsZero()` branch, build the replacement object from a genuinely cleared account (nonce 0, `types.EmptyCodeHash`, `types.EmptyRootHash`) rather than copying `obj.origin` wholesale, e.g. construct `o` via `newObject(s, obj.address, nil)` (or an explicit `types.NewEmptyStateAccount()`-based account) and then set only the balance, ensuring code/storage of the pre-destruct contract cannot be resurrected/read after finalisation.

### Proof of Concept
1. Deploy a contract `A` with a non-empty code body and one storage slot set (`SSTORE 1,1`).
2. In a block on the Amsterdam ruleset, send a transaction to `A` that (a) writes `SSTORE 2,2`, then (b) executes `SELFDESTRUCT(beneficiary=someOtherAddress)` while leaving `A`'s own balance non-zero (e.g., via a subsequent internal transfer back to `A`, or by relying on `this == beneficiary` semantics per `opSelfdestruct6780`).
3. After the block's `Finalise`/`IntermediateRoot`, query `A`'s state: `CodeHash`, `Nonce`, and slot `2` still return the pre-destruct values (code present, slot `2`=2) instead of the EIP-8264-mandated cleared account (`CodeHash = EmptyCodeHash`, `Nonce = 0`, storage empty). This can be observed directly by calling `StateDB.GetCode(A)`, `StateDB.GetNonce(A)`, and `StateDB.GetState(A, 2)` right after `Finalise` runs the `case obj.selfDestructed` branch in [1](#0-0) .

### Citations

**File:** core/state/statedb.go (L873-892)
```go
		switch {
		case obj.selfDestructed:
			// EIP-8264: accounts marked for self-destruction, instead of
			// being deleted, are modified as follows:
			// - nonce is reset to 0,
			// - balance is unchanged,
			// - code is cleared,
			// - all storage is cleared
			if !obj.Balance().IsZero() {
				o := newObject(s, obj.address, obj.origin)
				o.setBalance(new(uint256.Int).Set(obj.Balance()))
				s.setStateObject(o)
				s.markUpdate(addr)
			} else {
				delete(s.stateObjects, obj.address)
				s.markDelete(addr)
				if _, ok := s.stateObjectsDestruct[obj.address]; !ok {
					s.stateObjectsDestruct[obj.address] = obj
				}
			}
```

**File:** core/state/state_object.go (L95-111)
```go
// newObject creates a state object.
func newObject(db *StateDB, address common.Address, acct *types.StateAccount) *stateObject {
	origin := acct
	if acct == nil {
		acct = types.NewEmptyStateAccount()
	}
	return &stateObject{
		db:                 db,
		address:            address,
		origin:             origin,
		data:               *acct,
		originStorage:      make(Storage),
		dirtyStorage:       make(Storage),
		pendingStorage:     make(Storage),
		uncommittedStorage: make(Storage),
	}
}
```
