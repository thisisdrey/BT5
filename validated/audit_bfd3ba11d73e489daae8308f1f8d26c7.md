### Title
Self-destructed account under EIP-8264 retains stale nonce, code hash and storage root instead of being cleared - (File: core/state/statedb.go)

### Summary
This is a valid analog of the ArtGobblers "revived burnt token" class of bug: a field that the code claims to reset on destruction is in fact left pointing at the pre-destruction data, allowing a "destroyed" entity to keep its old identity/content alive.

### Finding Description
`finaliseAmsterdam` implements this fork's EIP-8264/EIP-8246 self-destruct semantics, documented in its own comment as: "nonce is reset to 0, balance is unchanged, code is cleared, all storage is cleared" [1](#0-0) .

When a self-destructed account has non-zero balance, the code does:
```go
o := newObject(s, obj.address, obj.origin)
o.setBalance(new(uint256.Int).Set(obj.Balance()))
s.setStateObject(o)
``` [2](#0-1) 

`newObject` sets `o.data = *acct` where `acct == obj.origin` when non-nil [3](#0-2) . `obj.origin` is the account's state *before* any mutation applied during the current block/transition (its pre-tx `Nonce`, `CodeHash`, and storage `Root`) [4](#0-3) . Only `Balance` is subsequently overwritten via `setBalance`; `Nonce`, `CodeHash` and `Root` are never reset to their empty values.

Additionally, this balance-surviving branch is the only one that does **not** register the address in `stateObjectsDestruct` (that only happens in the `balance == 0` branch and the EIP-161 empty-account branch) [5](#0-4) . `handleDestruction`, which is responsible for actually deleting an account's storage slots from the trie, only processes entries present in `stateObjectsDestruct` [6](#0-5) . Consequently, for a pre-existing contract (one whose `origin` is non-nil, i.e. it existed before the current transaction) that self-destructs with a non-zero balance:
- Its `data.Nonce` is copied from `origin.Nonce`, not reset to 0.
- Its `data.CodeHash` is copied from `origin.CodeHash`, not reset to `EmptyCodeHash`.
- Its `data.Root` is copied from `origin.Root`, not reset to `EmptyRootHash`, and its storage slots are never deleted from the trie because the address is missing from `stateObjectsDestruct`.

This is directly analogous to the ArtGobblers bug: the code's own contract states an entity is "destroyed"/"reset" (nonce 0, code cleared, storage cleared) but a stale reference (here `origin`, there `approvals`) is carried over, letting the destroyed entity's old code and storage silently "come back to life" attached to the surviving balance-only account, and the surviving account can still act as the old contract (same code hash, same storage) instead of becoming a pure EOA-like balance holder as the fork intends.

The existing unit tests (`TestEIP8246SelfdestructNoBurn`, `TestEIP8246Create2RecreatesBalanceOnly`) only cover contracts that are *created and self-destructed within the same transaction*, where `obj.origin` is `nil` (fresh account) and `newObject` correctly falls back to `types.NewEmptyStateAccount()` [7](#0-6) . None of the existing tests exercise a *pre-existing* contract (deployed in an earlier block, with non-zero nonce/code/storage) that self-destructs to itself/with non-zero balance under Amsterdam rules, which is exactly the path that hits the bug.

### Impact Explanation
This breaks the state-root equality the fork's own EIP-8264 spec defines: the persisted account after finalisation does not match what the code documents and what other conformant implementations of the same EIP would produce (nonce should be 0, code and storage should be empty). Any pre-existing contract with nonce/code/storage that self-destructs while retaining balance under Amsterdam rules will retain its old nonce, code hash, and storage root/slots. This is a state-root divergence (stateRoot differs from a compliant implementation) reachable by a single ordinary transaction on a mainnet-activated fork path, which is High/Critical depending on whether other client implementations of the same EIP compute the empty-fields variant (a genuine consensus split) — at minimum it is a persisted-state/executed-state mismatch relative to the documented semantics, and it also means the "destroyed" contract keeps functioning with its original code as if selfdestruct had no effect other than a balance top-up, defeating the fork's compute/storage-clearing guarantee.

### Likelihood Explanation
High likelihood of being triggered accidentally, and trivially triggerable deliberately: any already-deployed contract that performs `SELFDESTRUCT` while holding a non-zero balance (extremely common pattern, e.g. any contract with `balance > 0` calling `SELFDESTRUCT`) hits this exact code path once Amsterdam/EIP-8264 rules are active. No adversarial setup beyond a normal transaction is required.

### Recommendation
In the non-zero-balance branch of `finaliseAmsterdam`, construct the surviving object from an empty account template (not `obj.origin`) and only carry over the balance, e.g. `o := newObject(s, obj.address, nil)` followed by `o.setBalance(...)`, mirroring what already happens correctly for same-tx-created accounts. Additionally, register the address in `stateObjectsDestruct` (with the original `obj.origin` as `Origin`) in this branch too, so `handleDestruction` deletes the pre-existing storage slots from the trie, consistent with the "all storage is cleared" comment.

### Proof of Concept
Conceptual PoC (extends the pattern in `core/eip8246_test.go`):
1. Deploy contract `AA` in block 1 with `Nonce: 1`, non-empty `Code`, and storage slots set (mirrors the `TestDeleteRecreateAccount`/`TestDeleteRecreateSlots` fixtures already in the repo) [8](#0-7) .
2. Fund `AA` with a non-zero balance.
3. In block 2, send `AA` a call that triggers `SELFDESTRUCT` to itself (or to a beneficiary while `AA` still retains balance via the fork's no-burn rule), under `AmsterdamTime` activated config as in `TestEIP8246SelfdestructNoBurn` [9](#0-8) .
4. After block import, assert on the post-state: `state.GetNonce(AA)` (expected 0 per spec comment, actual = pre-tx nonce), `state.GetCodeSize(AA)` (expected 0, actual = original code size), and `state.GetState(AA, slot)` for a previously set slot (expected `common.Hash{}`, actual = original stored value) — all of which will fail, confirming the stale `origin` data (nonce/code/storage) survives instead of being cleared as documented in `core/state/statedb.go:874-881`.

### Citations

**File:** core/state/statedb.go (L93-98)
```go
	// This map holds 'deleted' objects. An object with the same address
	// might also occur in the 'stateObjects' map due to account
	// resurrection. The account value is tracked as the original value
	// before the transition. This map is populated at the transaction
	// boundaries.
	stateObjectsDestruct map[common.Address]*stateObject
```

**File:** core/state/statedb.go (L874-900)
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
				delete(s.stateObjects, obj.address)
				s.markDelete(addr)
				if _, ok := s.stateObjectsDestruct[obj.address]; !ok {
					s.stateObjectsDestruct[obj.address] = obj
				}
			}

		case rules.IsEIP158 && obj.empty():
			// EIP-161: a touched, empty account is removed.
			delete(s.stateObjects, obj.address)
			s.markDelete(addr)
			if _, ok := s.stateObjectsDestruct[obj.address]; !ok {
				s.stateObjectsDestruct[obj.address] = obj
			}
```

**File:** core/state/statedb.go (L1220-1256)
```go
func (s *StateDB) handleDestruction(rules params.Rules) (map[common.Hash]*AccountDelete, []*trienode.NodeSet, error) {
	var (
		nodes   []*trienode.NodeSet
		deletes = make(map[common.Hash]*AccountDelete)
	)
	for addr, prevObj := range s.stateObjectsDestruct {
		prev := prevObj.origin

		// The account was non-existent, and it's marked as destructed in the scope
		// of block. It can be either case (a) or (b) and will be interpreted as
		// null->null state transition.
		// - for (a), skip it without doing anything
		// - for (b), the resurrected account with nil as original will be handled afterwards
		if prev == nil {
			continue
		}
		// The account was existent, it can be either case (c) or (d).
		addrHash := prevObj.addrHash()
		op := &AccountDelete{
			Address: addr,
			Origin:  prev,
		}
		deletes[addrHash] = op

		// Short circuit if the origin storage was empty.
		if prev.Root == types.EmptyRootHash || s.db.Type().Is(TypeUBT) {
			continue
		}
		if rules.IsCancun {
			return nil, nil, fmt.Errorf("unexpected storage wiping, %x", addr)
		}
		// Remove storage slots belonging to the account.
		storages, storagesOrigin, set, err := s.deleteStorage(addrHash, prev.Root)
		if err != nil {
			return nil, nil, fmt.Errorf("failed to delete storage, err: %w", err)
		}
		op.Storages = storages
```

**File:** core/state/state_object.go (L96-111)
```go
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

**File:** core/blockchain_test.go (L2777-2807)
```go
func testDeleteRecreateAccount(t *testing.T, scheme string) {
	var (
		engine = ethash.NewFaker()

		// A sender who makes transactions, has some funds
		key, _  = crypto.HexToECDSA("b71c71a67e1177ad4e901695e1b4b9ee17ae16c6668d313eac2f96dbcda3f291")
		address = crypto.PubkeyToAddress(key.PublicKey)
		funds   = big.NewInt(1000000000000000)

		aa        = common.HexToAddress("0x7217d81b76bdd8707601e959454e3d776aee5f43")
		aaStorage = make(map[common.Hash]common.Hash)          // Initial storage in AA
		aaCode    = []byte{byte(vm.PC), byte(vm.SELFDESTRUCT)} // Code for AA (simple selfdestruct)
	)
	// Populate two slots
	aaStorage[common.HexToHash("01")] = common.HexToHash("01")
	aaStorage[common.HexToHash("02")] = common.HexToHash("02")

	gspec := &Genesis{
		Config: params.TestChainConfig,
		Alloc: types.GenesisAlloc{
			address: {Balance: funds},
			// The address 0xAAAAA selfdestructs if called
			aa: {
				// Code needs to just selfdestruct
				Code:    aaCode,
				Nonce:   1,
				Balance: big.NewInt(0),
				Storage: aaStorage,
			},
		},
	}
```

**File:** core/eip8246_test.go (L38-77)
```go
func TestEIP8246SelfdestructNoBurn(t *testing.T) {
	var (
		key1, _ = crypto.HexToECDSA("b71c71a67e1177ad4e901695e1b4b9ee17ae16c6668d313eac2f96dbcda3f291")
		addr1   = crypto.PubkeyToAddress(key1.PublicKey)
		config  = *params.MergedTestChainConfig
		signer  = types.LatestSigner(&config)
		engine  = beacon.New(ethash.NewFaker())
		value   = big.NewInt(1_000_000)
		slot    = common.BigToHash(big.NewInt(0x05))
		// Init code: SSTORE(5, 0x2a); ADDRESS (0x30); SELFDESTRUCT (0xff). The
		// created contract stores a value and self-destructs to itself during
		// its own creation transaction.
		initcode = []byte{0x60, 0x2a, 0x60, 0x05, 0x55, 0x30, 0xff}
	)
	// TODO: drop this hacky Amsterdam config initialization once the final
	// Amsterdam config is available (mirrors TestEthTransferLogs).
	config.AmsterdamTime = new(uint64)

	gspec := &Genesis{
		Config: &config,
		Alloc: withSystemContracts(types.GenesisAlloc{
			addr1: {Balance: newGwei(1_000_000_000)},
		}),
	}
	// The contract created by addr1's first (nonce 0) transaction.
	created := crypto.CreateAddress(addr1, 0)

	db, blocks, _ := GenerateChainWithGenesis(gspec, engine, 1, func(i int, b *BlockGen) {
		tx := types.MustSignNewTx(key1, signer, &types.DynamicFeeTx{
			ChainID:   gspec.Config.ChainID,
			Nonce:     0,
			To:        nil, // contract creation
			Gas:       1_000_000,
			GasFeeCap: newGwei(5),
			GasTipCap: newGwei(5),
			Value:     value,
			Data:      initcode,
		})
		b.AddTx(tx)
	})
```
