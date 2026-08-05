## Analog Identified [1](#0-0) 

### Title
Prefunded `CREATE2`/`CREATE1` contract addresses in `pallet-revive` permanently lose value to existential-deposit dust-burn with no restoration path on deployment - ([File: substrate/frame/revive/src/exec.rs])

### Summary
`pallet-revive` lets anyone compute the deterministic address of a not-yet-deployed contract via `address::create1`/`address::create2` and pre-fund that plain `AccountId` ahead of deployment, exactly the CREATE2 prefunding pattern described in the external report. If that account's balance is ever pushed below the existential deposit before the contract is instantiated there, `pallet-balances`' account mutation logic treats the *entire* remaining free balance as "dust" and destroys/forwards it via `DustLost`/`BurnedDebt` — not to the original depositor. When the contract is later instantiated at that exact address, `Deposit::init_contract` only mints a fresh existential deposit; nothing in the deployment path inspects, credits back, or otherwise "restores" value that previously existed at the address. This mirrors the original bug's core defect: value sent to a predicted-but-undeployed contract address has no restoration mechanism once the account becomes sub-ED ("dormant") prior to code being deployed.

### Finding Description
`create1`/`create2` deterministically compute a contract's future `H160` address from public inputs (deployer, nonce/salt, code) before any code exists there. [1](#0-0)  A test in the suite explicitly demonstrates the "send funds to the predicted address, then deploy" pattern working when the prefunded amount equals the existential deposit exactly: [2](#0-1) 

Until the contract is deployed, the address is a normal `AccountId` governed purely by `pallet-balances` existential-deposit rules. `try_mutate_account` computes `maybe_dust` whenever the account's free balance drops below ED with zero reserved balance, and that *entire* remaining balance — not just the sub-ED remainder — is dropped as dust and handled via `handle_raw_dust`, emitting `DustLost`/`BurnedDebt` with no credit back to the original sender: [3](#0-2) 

When the contract eventually gets instantiated at the same address (via `push_frame`/`new_frame` using `address::create1`/`create2`), the constructor path only calls `T::Deposit::init_contract` if the account doesn't already exist, which unconditionally mints exactly one fresh existential deposit and nothing more: [4](#0-3) [5](#0-4) 

There is no guard anywhere in `ContractInfo::new` or the instantiate path that checks whether the target address previously held (and lost) a larger balance, nor any mechanism to make a depositor whole — precisely the missing "restorationTX" class of defect from the report, just expressed through the standard ED/dust machinery instead of a bespoke `verifyRestorationProof` check. [6](#0-5) 

### Impact Explanation
Any value held by a `pallet-revive` contract address prior to its deployment is only as safe as that account's ability to stay at-or-above the existential deposit. Any event that pushes it below ED (a withdrawal, fee debit, or partial reserved-balance repatriation touching that address) destroys the account's *entire* remaining balance as dust, permanently and irrecoverably, before the contract that was supposed to receive/use those funds is ever deployed. This is a permanent, unbacked loss of user funds with no remediation path — the deployment logic (`init_contract`) is unaware that value was ever there and cannot restore it.

### Likelihood Explanation
The prefunding pattern (funding a `CREATE2` address before deployment) is explicitly supported and tested by the codebase itself, so it is a realistic, encouraged usage pattern rather than a contrived edge case. The failure mode only requires an ordinary balance-reducing operation on the not-yet-deployed address to cross the ED threshold — no privileged actor, validator, relayer, or governance action is required, satisfying the "unprivileged, public-path" bar for this analog.

### Recommendation
Before allowing any operation that would reap/dust-burn an account, check whether an outstanding `CREATE1`/`CREATE2` predicted-address reservation exists for it, or otherwise disallow reaping of addresses that are still eligible to become contract accounts. Alternatively, route any would-be dust from such addresses to a recoverable holding account rather than burning it outright, and have `init_contract`/`ContractInfo::new` consult that holding account to top up the newly-deployed contract when funds were previously dust-removed from its address.

### Proof of Concept
1. Compute `addr = address::create2(&deployer, &code, &input_data, &salt)` off-chain (deterministic, public computation) for a contract to be deployed later.
2. Transfer a balance `X` (`> ED`) to the plain `AccountId` corresponding to `addr` via a normal signed transfer, creating the account (mirrors `existential_deposit_shall_not_be_charged_twice`, `substrate/frame/revive/src/tests/pvm.rs:5481-5511`, but with `X` intentionally set above the minimal ED-only amount used in that test).
3. Before the contract is instantiated, cause any operation that reduces that account's free balance below ED (e.g., a signed transfer-out from the address itself, or a reserved-balance repatriation that targets it) — per `substrate/frame/balances/src/lib.rs:1130-1141`, the entire remaining balance becomes "dust" and is burned/forwarded via `DustLost`, not returned to the original depositor.
4. Deploy the contract with matching `salt`/`code` to `addr` via `instantiate_with_code`, per `substrate/frame/revive/src/lib.rs:1280-1314`, executing `Deposit::init_contract` in `substrate/frame/revive/src/deposit_payment.rs:161-166`, which mints only a fresh ED.
5. Observe that the value destroyed in step 3 is never recovered — the original depositor's funds are permanently lost with no code path to reclaim them.

### Citations

**File:** substrate/frame/revive/src/address.rs (L263-285)
```rust
/// Determine the address of a contract using CREATE semantics.
pub fn create1(deployer: &H160, nonce: u64) -> H160 {
	let mut list = rlp::RlpStream::new_list(2);
	list.append(&deployer.as_bytes());
	list.append(&nonce);
	let hash = keccak_256(&list.out());
	H160::from_slice(&hash[12..])
}

/// Determine the address of a contract using the CREATE2 semantics.
pub fn create2(deployer: &H160, code: &[u8], input_data: &[u8], salt: &[u8; 32]) -> H160 {
	let init_code_hash = {
		let init_code: Vec<u8> = code.into_iter().chain(input_data).cloned().collect();
		keccak_256(init_code.as_ref())
	};
	let mut bytes = [0; 85];
	bytes[0] = 0xff;
	bytes[1..21].copy_from_slice(deployer.as_bytes());
	bytes[21..53].copy_from_slice(salt);
	bytes[53..85].copy_from_slice(&init_code_hash);
	let hash = keccak_256(&bytes);
	H160::from_slice(&hash[12..])
}
```

**File:** substrate/frame/revive/src/tests/pvm.rs (L5481-5511)
```rust
#[test]
fn existential_deposit_shall_not_be_charged_twice() {
	let (code, _) = compile_module("dummy").unwrap();

	let salt = [0u8; 32];

	ExtBuilder::default().build().execute_with(|| {
		let _ = <Test as Config>::Currency::set_balance(&ALICE, 100_000_000_000_000);
		let callee_addr = create2(
			&ALICE_ADDR,
			&code,
			&[0u8; 0], // empty input
			&salt,
		);
		let callee_account = <Test as Config>::AddressMapper::to_account_id(&callee_addr);

		// first send funds to callee_addr
		let _ = <Test as Config>::Currency::set_balance(&callee_account, Contracts::min_balance());
		assert_eq!(get_balance(&callee_account), Contracts::min_balance());

		// then deploy contract to callee_addr using create2
		let Contract { addr, .. } = builder::bare_instantiate(Code::Upload(code.clone()))
			.salt(Some(salt))
			.build_and_unwrap_contract();

		assert_eq!(callee_addr, addr);

		// check we charged ed only 1 time
		assert_eq!(get_balance(&callee_account), Contracts::min_balance());
	});
}
```

**File:** substrate/frame/balances/src/lib.rs (L1126-1152)
```rust
				// some dust should be dropped.
				//
				// We should never be dropping if reserved is non-zero. Reserved being non-zero
				// should imply that we have a consumer ref, so this is economically safe.
				let ed = Self::ed();
				let maybe_dust = if account.free < ed && account.reserved.is_zero() {
					if account.free.is_zero() {
						None
					} else {
						Some(account.free)
					}
				} else {
					*maybe_account = Some(account);
					None
				};
				Ok((maybe_endowed, maybe_dust, result))
			});
			result.map(|(maybe_endowed, maybe_dust, result)| {
				if let Some(endowed) = maybe_endowed {
					Self::deposit_event(Event::Endowed {
						account: who.clone(),
						free_balance: endowed,
					});
				}
				if let Some(amount) = maybe_dust {
					Pallet::<T, I>::deposit_event(Event::DustLost { account: who.clone(), amount });
				}
```

**File:** substrate/frame/revive/src/exec.rs (L1343-1348)
```rust
			// We need to make sure that the contract's account exists before calling its
			// constructor.
			if entry_point == ExportedFunction::Constructor {
				if !frame_system::Pallet::<T>::account_exists(&account_id) {
					T::Deposit::init_contract(account_id)?;
				}
```

**File:** substrate/frame/revive/src/deposit_payment.rs (L161-166)
```rust
	fn init_contract(to: &T::AccountId) -> DispatchResult {
		let ed = T::Currency::minimum_balance();
		T::Currency::mint_into(to, ed)?;
		T::Currency::deactivate(ed);
		Ok(())
	}
```

**File:** substrate/frame/revive/src/storage.rs (L196-212)
```rust
	pub fn new(
		address: &H160,
		nonce: T::Nonce,
		code_hash: sp_core::H256,
	) -> Result<Self, DispatchError> {
		if <AccountInfo<T>>::is_contract(address) {
			return Err(Error::<T>::DuplicateContract.into());
		}

		// Reject reuse of an address whose previous occupant still has unflushed
		// `NativeDepositOf` rows in the deletion queue. The on_idle drain will eventually
		// clear them; until it does, instantiating here would let the new contract inherit
		// stale per-payer entitlements.
		let account_id = T::AddressMapper::to_fallback_account_id(address);
		if NativeDepositOf::<T>::iter_prefix(&account_id).next().is_some() {
			return Err(Error::<T>::PendingDepositCleanup.into());
		}
```
