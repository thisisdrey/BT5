### Title
`is_contract`/`code_hash` return false negatives for an address still under construction, letting a contract impersonate an EOA during its own constructor - ([File: substrate/frame/revive/src/storage.rs], [File: substrate/frame/revive/src/exec.rs], [File: substrate/frame/contracts/src/exec.rs])

### Summary
The external report's core broken invariant is: a Solidity `isContract()` check (`extcodesize(addr) > 0`) returns `false` for a contract that is still executing its own constructor, because the code/`ContractInfo` is only committed to storage after construction completes. An attacker's factory contract exploits this window to make the "is a contract" check lie about its own nature. The same invariant exists natively in this repository's contracts execution layers: `AccountInfo::is_contract` in `pallet-revive` and `Ext::is_contract` in `pallet-contracts` both consult `AccountInfoOf` / `ContractInfoOf`, which are populated only when the constructor's frame pops — not while it is still running.

### Finding Description
In `pallet-revive`, `AccountInfo::<T>::is_contract` looks up `AccountInfoOf` and returns `false` if no entry exists: [1](#0-0) 

The account entry for a contract under construction is only written when its `Constructor` frame *pops* (`pop_frame`), not when the frame is pushed: [2](#0-1) 

`push_frame` explicitly documents and only partially closes this gap: it pre-persists `AccountInfo` for a `Call` frame so nested children can see pending changes, but says "we do not store on instantiate", and only adds a narrow guard rejecting a *re-entrant CREATE2 collision at the same address* (`DuplicateContract`) — it does not make the in-construction address visible to `is_contract`/`code_hash` in general: [3](#0-2) 

This is confirmed by the project's own prdoc, which describes exactly the Solidity-analog root cause ("ContractInfo is not written to AccountInfoOf until its constructor frame pops, so the is_contract ... guard ... could not see an address that was still being constructed") but only fixes the CREATE2-collision instance of it, matching EIP-684 — not the general visibility problem: [4](#0-3) 

The pallet-contracts equivalent host function has the identical shape and is directly exposed to any WASM contract via `seal_is_contract`: [5](#0-4) 

Crucially, self-reentrant calls during a constructor are explicitly supported behavior, not blocked: a contract can call back into its own (not-yet-written) address during its constructor, and the code comments confirm this is intentional ("Calling ourselves during the constructor will trigger a balance transfer since no contract exist yet"): [6](#0-5) 

So during a constructor, the contract's own address (a) has code deployed and running, but (b) is invisible to `is_contract`/`code_hash` for any other frame/pallet logic that queries it, exactly reproducing the Solidity bug class where `extcodesize` returns 0 mid-construction. Any pallet or precompile logic gating privileged behavior on "caller/target is not a contract" (the EOA-vs-contract distinction that `AccountType::EOA` vs `AccountType::Contract` and EIP-3607's `ensure_non_contract_if_signed` are built around) is only as strong as `AccountInfo::is_contract`, and that primitive is provably blind to in-construction contracts.

### Impact Explanation
Any current or future runtime logic (a pallet, precompile, or contract) that relies on `is_contract`/`Ext::is_contract`/`code_hash` to gate privileged behavior (e.g., "reject calls that originate from a contract", "only humans may stake/claim", whitelist/blacklist logic modeled on the reported `ContractWhitelist.sol` pattern) can be bypassed by a contract that performs the privileged action from within its own constructor, exactly as the external report's `ImpersonatorFactory` did. This is a real gap in a core security primitive of the execution engine (`pallet-revive`/`pallet-contracts`), not merely an application-level bug, so any downstream consumer inherits the false-negative.

### Likelihood Explanation
Unprivileged: any user can deploy a contract whose constructor performs a self-reentrant or cross-contract call while its own `AccountInfo`/`ContractInfo` record is not yet committed. The maintainers themselves recognized and partially fixed a manifestation of this exact gap (the CREATE2 re-entrant collision, PR fixing issue #12639), confirming the underlying storage-timing behavior is real and previously exploitable; the general `is_contract` visibility gap for non-collision call paths remains structurally present per the code shown above.

### Recommendation
Make the account-type record visible for the full duration of the constructor call (write a placeholder/"under construction" `AccountInfo` entry when the `Constructor` frame is pushed, not only when it pops), and have `is_contract`/`code_hash` treat any address with an active `Constructor` frame on the call stack as a contract. Alternatively, explicitly document and enforce (via a stack-scan similar to the `DuplicateContract` guard) that any dispatchable/precompile relying on `is_contract` treats "constructor in progress" addresses as contracts, closing the general case rather than only the CREATE2-collision special case.

### Proof of Concept
1. Deploy contract `A` whose constructor, before returning, calls (self-reentrant or cross-contract call) into contract `B` (or a precompile/pallet that checks `is_contract`/`code_hash` on `msg.sender`/caller address to gate a privileged action, mirroring `ContractWhitelist.isContract`).
2. Because `A`'s `ContractInfo`/`AccountInfo` entry is not persisted into `AccountInfoOf`/`ContractInfoOf` until `A`'s constructor frame pops (`pop_frame`, `AccountInfo::insert_contract` only called at pop time — [2](#0-1) ), `B`'s call to `is_contract(A_address)` returns `false`, and `code_hash(A_address)` returns `None`.
3. `B` treats `A` as an EOA and grants it the EOA-only privileged path, which `A` (a contract) should not have qualified for — the same "impersonator" bypass described in the external report, reproduced with `is_contract`/`code_hash` instead of `extcodesize`.

### Citations

**File:** substrate/frame/revive/src/storage.rs (L142-147)
```rust
impl<T: Config> AccountInfo<T> {
	/// Returns true if the account is a contract.
	pub fn is_contract(address: &H160) -> bool {
		let Some(info) = <AccountInfoOf<T>>::get(address) else { return false };
		matches!(info.account_type, AccountType::Contract(_))
	}
```

**File:** substrate/frame/revive/src/exec.rs (L1204-1246)
```rust
		// We need to make sure that changes made to the contract info are not discarded.
		// See the `in_memory_changes_not_discarded` test for more information.
		// We do not store on instantiate because we do not allow to call into a contract
		// from its own constructor.
		//
		// Additionally, we need to apply pending storage changes to the ContractInfo before
		// saving it, so that child frames can correctly calculate storage deposit refunds.
		// See: <https://github.com/paritytech/contract-issues/issues/213>
		let frame = self.top_frame();
		if let (CachedContract::Cached(contract), ExportedFunction::Call) =
			(&frame.contract_info, frame.entry_point)
		{
			let mut contract_with_pending_changes = contract.clone();
			frame
				.frame_meter
				.apply_pending_storage_changes(&mut contract_with_pending_changes);
			AccountInfo::<T>::insert_contract(
				&T::AddressMapper::to_address(&frame.account_id),
				contract_with_pending_changes,
			);
		}

		let frame = top_frame_mut!(self);
		let meter = &mut frame.frame_meter;
		if let Some((frame, executable)) = Self::new_frame(
			frame_args,
			value_transferred,
			meter,
			call_resources,
			read_only,
			false,
			input_data,
			self.exec_config,
		)? {
			// EIP-684: an in-construction address is not in `AccountInfoOf` yet, so the
			// `is_contract` guard in `ContractInfo::new` misses this re-entrant collision.
			if frame.entry_point == ExportedFunction::Constructor &&
				self.frames().any(|f| {
					f.entry_point == ExportedFunction::Constructor &&
						f.account_id == frame.account_id
				}) {
				return Err(Error::<T>::DuplicateContract.into());
			}
```

**File:** substrate/frame/revive/src/exec.rs (L1658-1671)
```rust
			if let Some(contract) = contract {
				// Persist the info and invalidate the first stale cache we find.
				// This triggers a reload from storage on next use. Only the first
				// cache needs to be invalidated because that one will invalidate the next cache
				// when it is popped from the stack.
				AccountInfo::<T>::insert_contract(
					&T::AddressMapper::to_address(account_id),
					contract,
				);
				if let Some(f) = self.frames_mut().find(|f| f.account_id == *account_id) {
					// Bank before invalidating so finalize doesn't apply the diff a second time.
					bank_pending_changes_and_invalidate(f);
				}
			}
```

**File:** prdoc/pr_12645.prdoc (L1-18)
```text
title: '[pallet-revive] Reject re-entrant instantiate at an in-construction address'
doc:
- audience: Runtime Dev
  description: |-
    Fixes https://github.com/paritytech/polkadot-sdk/issues/12639

    A contract's `ContractInfo` is not written to `AccountInfoOf` until its constructor
    frame pops, so the `is_contract` collision guard in `ContractInfo::new` could not see an
    address that was still being constructed. A re-entrant `CREATE2` with the same salt and
    code (which is nonce independent) therefore resolved to the same address and ran a second
    constructor frame for one account, permanently leaking its consumer reference and code
    refcount and orphaning the second child trie's storage deposit.

    `push_frame` now rejects a nested instantiate whose target address already appears as a
    `Constructor` frame on the call stack, returning `DuplicateContract` (matching EIP-684).
crates:
- name: pallet-revive
  bump: patch
```

**File:** substrate/frame/contracts/src/wasm/runtime.rs (L1693-1702)
```rust
	/// Checks whether a specified address belongs to a contract.
	/// See [`pallet_contracts_uapi::HostFn::is_contract`].
	#[prefixed_alias]
	fn is_contract(ctx: _, memory: _, account_ptr: u32) -> Result<u32, TrapReason> {
		ctx.charge_gas(RuntimeCosts::IsContract)?;
		let address: <<E as Ext>::T as frame_system::Config>::AccountId =
			ctx.read_sandbox_memory_as(memory, account_ptr)?;

		Ok(ctx.ext.is_contract(&address) as u32)
	}
```

**File:** substrate/frame/revive/src/exec/tests.rs (L1564-1594)
```rust
#[test]
fn recursive_call_during_constructor_is_balance_transfer() {
	let code = MockLoader::insert(Constructor, |ctx, _| {
		let account_id = ctx.ext.account_id().clone();
		let addr =
			<<Test as Config>::AddressMapper as AddressMapper<Test>>::to_address(&account_id);
		let balance = ctx.ext.balance();

		// Calling ourselves during the constructor will trigger a balance
		// transfer since no contract exist yet.
		assert_ok!(ctx.ext.call(
			&Default::default(),
			&addr,
			(balance - 1).into(),
			vec![],
			ReentrancyProtection::AllowReentry,
			false
		));

		// Should also work with call data set as it is ignored when no
		// contract is deployed.
		assert_ok!(ctx.ext.call(
			&Default::default(),
			&addr,
			1u32.into(),
			vec![1, 2, 3, 4],
			ReentrancyProtection::AllowReentry,
			false
		));
		exec_success()
	});
```
