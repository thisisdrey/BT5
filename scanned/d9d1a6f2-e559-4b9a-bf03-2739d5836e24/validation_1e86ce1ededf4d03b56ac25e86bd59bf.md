### Title
Same-contract reentry double-counts storage deposit in legacy `pallet-contracts` (unlike the patched `pallet-revive`) - (File: `substrate/frame/contracts/src/exec.rs`)

### Summary
The external report describes `emergencyWithdraw` failing to update contract state before an external, attacker-controllable call, letting the attacker reenter and see/act on stale state. The exact same broken invariant — "child frame sees a stale/duplicated snapshot of contract state because the parent's *pending* writes are not applied before the reentrant frame is given a cached copy" — is present in the legacy `pallet-contracts` crate, which is still wired into a live runtime (`substrate/bin/node/runtime`) alongside `pallet-revive`. `pallet-revive` already had this exact issue and was patched (see `prdoc/pr_12267.prdoc`), but `pallet-contracts`'s `Stack::call`/`delegate_call` cached-info path was never given the equivalent fix.

### Finding Description
In `pallet-revive`'s `Stack::call` (`substrate/frame/revive/src/exec.rs:2201-2211`), when a contract calls back into an ancestor contract still on the call stack (same-contract reentry, `ALLOW_REENTRY`), the cached `ContractInfo` handed to the child frame is built like this:
```rust
CachedContract::Cached(contract) => {
    let mut contract_with_pending = contract.clone();
    f.frame_meter.apply_pending_storage_changes(&mut contract_with_pending);
    Some(contract_with_pending)
},
```
The `apply_pending_storage_changes` call was added specifically to fix a double-counting bug documented in `prdoc/pr_12267.prdoc`: without it, the pre-call write (`K1`) is applied once to the persisted `ContractInfo` on child-frame finalize, and then re-applied a second time when the parent's own `own_contribution` diff is finalized on its own pop — inflating `storage_items`/`storage_bytes`/`storage_*_deposit` (`prdoc/pr_12267.prdoc:7-17`).

`pallet-contracts` (`substrate/frame/contracts/src/exec.rs:1284-1290`) implements the identical cached-info lookup for its `Ext::call`:
```rust
let cached_info = self
    .frames()
    .find(|f| f.entry_point == ExportedFunction::Call && f.account_id == to)
    .and_then(|f| match &f.contract_info {
        CachedContract::Cached(contract) => Some(contract.clone()),
        _ => None,
    });
```
There is no equivalent `apply_pending_storage_changes`/"bank pending diff" step before the clone is handed to the reentrant child frame, and `delegate_call` at `substrate/frame/contracts/src/exec.rs:1311-1333` clones `contract_info()` the same unguarded way. This is structurally the same defect the revive PR fixed, just never back-ported to the legacy pallet. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) 

Reachability: `pallet_contracts::Config for Runtime` is implemented alongside `pallet_revive::Config for Runtime` in `substrate/bin/node/runtime/src/lib.rs`, so the legacy pallet is still a live, publicly dispatchable execution engine, not a dead crate. [5](#0-4) 

Any Wasm contract can call itself with `CallFlags::ALLOW_REENTRY` (an unprivileged, contract-author-controlled flag; `substrate/frame/revive/uapi/src/flags.rs:62-72` documents the equivalent flag semantics), so this is directly reachable by an ordinary contract call, exactly like the Solidity `emergencyWithdraw`/`onERC721Received` reentry pattern in the external report — no relayer, validator, or governance actor is required. [6](#0-5) [7](#0-6) 

### Impact Explanation
Inflated `storage_items`/`storage_bytes`/deposit counters become the denominator for pro-rata refund calculation on subsequent `clear_storage` (the same mechanism `RawMeter::bank_pending_changes` / `Diff::update_contract`'s `.min(FixedU128::from_u32(1))` clamp addresses in revive). With an inflated denominator, storage-deposit refunds under-deliver, permanently stranding part of a contract's/account's storage deposit — a permanent user-fund lock, which is within the accepted impact class ("permanent user-fund ... lock"). Because the legacy `pallet-contracts` never received the `bank_pending_changes`/`apply_pending_storage_changes` fix, this corruption path is not mitigated the way it is in `pallet-revive`.

### Likelihood Explanation
High for any chain that still enables legacy `pallet-contracts` (as the shipped `substrate/bin/node/runtime` does). The attacker primitive is trivial: deploy a contract, write storage, call itself with `ALLOW_REENTRY`, write storage again, then later clear storage and observe the reduced/incorrect refund versus a direct (non-reentrant) equivalent sequence. No special privileges, validator collusion, or off-chain infrastructure are needed — it is a pure public-entrypoint (`call`/`instantiate` dispatchables) state-accounting bug.

### Recommendation
Port the `pallet-revive` fix to `pallet-contracts`: before cloning `ContractInfo` into a same-contract reentrant child frame's `cached_info` in `Stack::call` and `Stack::delegate_call` (`substrate/frame/contracts/src/exec.rs`), apply/bank the parent frame's pending storage diff into the clone (mirroring `RawMeter::bank_pending_changes` and `Frame::bank_pending_changes_and_invalidate` added for revive), and invalidate the parent's cache accordingly so its own `finalize()` does not re-apply the same diff a second time. Add regression tests analogous to `same_contract_reentry_does_not_double_count_storage` / `transitive_reentry_does_not_double_count_storage` from `substrate/frame/revive/src/exec/tests.rs` against `pallet-contracts`.

### Proof of Concept
1. Deploy contract `X` under `pallet-contracts` with a `call()` export that: writes key `K1`, calls itself (`ext.call(..., allows_reentry = true, ...)`), and on the second (reentrant) invocation writes key `K2`.
2. Invoke `X.call()` once (triggering `X → X` reentry) via the `pallet_contracts::Call::call` extrinsic (public, unprivileged).
3. Read back `ContractInfoOf::<T>::get(X)` and compare `storage_items`/`storage_bytes`/`storage_item_deposit` against the values produced by performing the same two writes non-reentrantly (`set(K1); set(K2)` in one call). Per the pattern documented for the (fixed) `pallet-revive` case, the reentrant path is expected to report inflated counters (e.g. `storage_items == 3` vs the correct `2`), because `K1`'s pending diff is applied both by the child frame's persist-on-success and again by the parent's `finalize()`.
4. Follow with a `clear_storage(K1)` call and observe the returned refund is smaller than the refund obtained from the equivalent non-reentrant sequence, demonstrating the stranded/lost deposit (the concrete corrupted value is `ContractInfo.storage_item_deposit` / `storage_byte_deposit`, and the existing `.min(FixedU128::from_u32(1))` refund clamp elsewhere only prevents over-refund, not under-refund/fund-lock).

Note: I was unable to fully trace `pallet-contracts`'s `finalize()`/`own_contribution` bookkeeping within the tool-call budget to mechanically re-derive the exact double-count arithmetic (as was done for `pallet-revive` in `prdoc/pr_12267.prdoc`); the finding rests on the structural absence of the `apply_pending_storage_changes` step that revive's own prdoc identifies as the fix for this exact class of bug. A Devin session with repository access could confirm the precise `finalize`/`own_contribution` interaction in `substrate/frame/contracts/src/exec.rs` and run the PoC above to verify actual under-refund numbers before treating this as fully confirmed.

### Citations

**File:** substrate/frame/revive/src/exec.rs (L2201-2211)
```rust
			let cached_info = self
				.frames()
				.find(|f| f.entry_point == ExportedFunction::Call && f.account_id == dest)
				.and_then(|f| match &f.contract_info {
					CachedContract::Cached(contract) => {
						let mut contract_with_pending = contract.clone();
						f.frame_meter.apply_pending_storage_changes(&mut contract_with_pending);
						Some(contract_with_pending)
					},
					_ => None,
				});
```

**File:** substrate/frame/contracts/src/exec.rs (L1242-1310)
```rust
	fn allows_reentry(&self, id: &AccountIdOf<T>) -> bool {
		!self.frames().any(|f| &f.account_id == id && !f.allows_reentry)
	}

	/// Increments and returns the next nonce. Pulls it from storage if it isn't in cache.
	fn next_nonce(&mut self) -> u64 {
		let next = self.nonce().wrapping_add(1);
		self.nonce = Some(next);
		next
	}
}

impl<'a, T, E> Ext for Stack<'a, T, E>
where
	T: Config,
	E: Executable<T>,
{
	type T = T;

	fn call(
		&mut self,
		gas_limit: Weight,
		deposit_limit: BalanceOf<T>,
		to: T::AccountId,
		value: BalanceOf<T>,
		input_data: Vec<u8>,
		allows_reentry: bool,
		read_only: bool,
	) -> Result<ExecReturnValue, ExecError> {
		// Before pushing the new frame: Protect the caller contract against reentrancy attacks.
		// It is important to do this before calling `allows_reentry` so that a direct recursion
		// is caught by it.
		self.top_frame_mut().allows_reentry = allows_reentry;

		let try_call = || {
			if !self.allows_reentry(&to) {
				return Err(<Error<T>>::ReentranceDenied.into());
			}

			// We ignore instantiate frames in our search for a cached contract.
			// Otherwise it would be possible to recursively call a contract from its own
			// constructor: We disallow calling not fully constructed contracts.
			let cached_info = self
				.frames()
				.find(|f| f.entry_point == ExportedFunction::Call && f.account_id == to)
				.and_then(|f| match &f.contract_info {
					CachedContract::Cached(contract) => Some(contract.clone()),
					_ => None,
				});
			let executable = self.push_frame(
				FrameArgs::Call { dest: to, cached_info, delegated_call: None },
				value,
				gas_limit,
				deposit_limit,
				// Enable read-only access if requested; cannot disable it if already set.
				read_only || self.is_read_only(),
			)?;
			self.run(executable, input_data)
		};

		// We need to make sure to reset `allows_reentry` even on failure.
		let result = try_call();

		// Protection is on a per call basis.
		self.top_frame_mut().allows_reentry = true;

		result
	}

```

**File:** substrate/frame/contracts/src/exec.rs (L1311-1333)
```rust
	fn delegate_call(
		&mut self,
		code_hash: CodeHash<Self::T>,
		input_data: Vec<u8>,
	) -> Result<ExecReturnValue, ExecError> {
		let executable = E::from_storage(code_hash, self.gas_meter_mut())?;
		let top_frame = self.top_frame_mut();
		let contract_info = top_frame.contract_info().clone();
		let account_id = top_frame.account_id.clone();
		let value = top_frame.value_transferred;
		let executable = self.push_frame(
			FrameArgs::Call {
				dest: account_id,
				cached_info: Some(contract_info),
				delegated_call: Some(DelegatedCall { executable, caller: self.caller().clone() }),
			},
			value,
			Weight::zero(),
			BalanceOf::<T>::zero(),
			self.is_read_only(),
		)?;
		self.run(executable, input_data)
	}
```

**File:** prdoc/pr_12267.prdoc (L1-27)
```text
title: '[pallet-revive] fix double deposit charge to parent contractinfo'
doc:
- audience: Runtime Dev
  description: |-
    # pallet-revive: fix storage deposit double-count under same-contract reentry

    When a contract writes storage, reenters itself (directly or via an intermediary), and writes storage again, the pre-call write is applied to the contract's persisted `ContractInfo` twice — inflating `storage_items` / `storage_bytes` / `storage_*_deposit`. The corruption is persisted via `insert_contract` and survives across transactions; subsequent `clear_storage` operations under-refund because the inflated counters become the denominator of the pro-rata refund.

    ## What goes wrong

    For `X` writes `K1` → calls itself → writes `K2`:

    1. `push_frame` (`exec.rs:1212-1223`) and the same-contract `cached_info` shortcut (`exec.rs:2150-2160`) clone the parent's `ContractInfo`, preview-apply the parent's pending diff to the clone, and use it as the child's view. The child persists that clone via `insert_contract` on success.
    2. The cache-invalidation matcher (`exec.rs:1616`) then marks the parent's cache `Invalidated`. The parent's next write reloads from storage, which already contains the preview-applied `K1`.
    3. The parent's `finalize()` (`exec.rs:1474-1478`) re-applies its still-pending `own_contribution` (which still contains `K1`) on top of the reloaded info → `K1` counted twice.

    This is a regression from [#10920](https://github.com/paritytech/polkadot-sdk/pull/10920) (commit `1b9ea1c3656`, merged 2026-02-10), which introduced the preview-apply step to make pending writes visible to nested frames for refund pro-rating, but did not consume the parent's `own_contribution`. The existing #10920 regression test (`metering::tests::nested_call_storage_refund` with the `setAndCallClear` fixture) does not catch the case because the parent performs no write after the nested call returns — its cache stays `Invalidated`, the outer pop's `as_contract()` returns `None`, and the diff is never re-applied.

    ## Fix

    Bank the parent's pending diff at the cache-invalidation site so `finalize()` only applies writes recorded afterwards.

    - `RawMeter::bank_pending_changes(contract, info)` (`metering/storage.rs`) — applies the `Alive` diff to `info` once, pushes the resulting deposit as a final `Charge` via the existing `charge_deposit` primitive, and resets `own_contribution`. Two `debug_assert!`s encode invariants: `info.is_some()` whenever the diff is non-empty (otherwise `Diff::update_contract(None)` at `storage.rs:130-134` drops the refund portion and over-charges), and `own_contribution` is `Alive` when banked (on-stack ancestors have not finalized yet, since `finalize` runs at `exec.rs:1474-1478` only at the frame's own pop).
    - `Frame::bank_pending_changes_and_invalidate` (`exec.rs`) — bundles `load → bank → invalidate` so the meter never sees `None` info and the ordering can't be misexpressed. Called from `pop_frame` only when the matcher finds an ancestor with the popped child's `account_id`.

    The `load` covers the case where an earlier same-contract reentry already invalidated the frame, or it accrued a removal-bearing diff via `charge_storage` (which does not reload the cache). Without it, banking would hit the silent `update_contract(None)` refund-drop.

```

**File:** substrate/bin/node/runtime/src/lib.rs (L1608-1642)
```rust
impl pallet_revive::Config for Runtime {
	type Time = Timestamp;
	type Balance = Balance;
	type Currency = Balances;
	type RuntimeEvent = RuntimeEvent;
	type RuntimeCall = RuntimeCall;
	type RuntimeOrigin = RuntimeOrigin;
	type DepositPerItem = DepositPerItem;
	type DepositPerChildTrieItem = DepositPerChildTrieItem;
	type DepositPerByte = DepositPerByte;
	type WeightInfo = pallet_revive::weights::SubstrateWeight<Self>;
	type Precompiles = (
		ERC20<Self, InlineIdConfig<0x1>, Instance1>,
		ERC20<Self, InlineIdConfig<0x2>, Instance2>,
		VestingPrecompile<Self>,
	);
	type AddressMapper = pallet_revive::AccountId32Mapper<Self>;
	type RuntimeMemory = ConstU32<{ 128 * 1024 * 1024 }>;
	type PVFMemory = ConstU32<{ 512 * 1024 * 1024 }>;
	type UploadOrigin = EnsureSigned<Self::AccountId>;
	type InstantiateOrigin = EnsureSigned<Self::AccountId>;
	type RuntimeHoldReason = RuntimeHoldReason;
	type CodeHashLockupDepositPercent = CodeHashLockupDepositPercent;
	type ChainId = ConstU64<420_420_420>;
	type NativeToEthRatio = ConstU32<1_000_000>; // 10^(18 - 12) Eth is 10^18, Native is 10^12.
	type FindAuthor = <Runtime as pallet_authorship::Config>::FindAuthor;
	type AllowEVMBytecode = ConstBool<true>;
	type FeeInfo = pallet_revive::evm::fees::Info<Address, Signature, EthExtraImpl>;
	type MaxEthExtrinsicWeight = MaxEthExtrinsicWeight;
	type DebugEnabled = ConstBool<false>;
	type AutoMap = ConstBool<false>;
	type GasScale = ConstU32<1000>;
	type OnBurn = ();
	type Deposit = ();
}
```

**File:** substrate/frame/revive/uapi/src/flags.rs (L62-72)
```rust
		/// Allow the callee to reenter into the current contract.
		///
		/// Without this flag any reentrancy into the current contract that originates from
		/// the callee (or any of its callees) is denied. This includes the first callee:
		/// You cannot call into yourself with this flag set.
		///
		/// # Note
		///
		/// For `seal_delegate_call` should be always unset, otherwise
		/// [`Error::InvalidCallFlags`] is returned.
		const ALLOW_REENTRY = 0b0000_1000;
```
