## Analysis: `pallet-revive::dispatch_as_fallback_account` unrestricted call dispatch under a hash-derived identity

### Core broken invariant in the external report
`walletAdminCall()` checked only that the first parameter of a forwarded call equals `msg.sender`, but placed no restriction on *which* function of `ProxyAdmin` could be invoked. Since `transferOwnership(address newOwner)` also takes an address as its first parameter, the caller could pass itself and hijack ownership — a public wrapper that binds identity but not call-selector, letting an unprivileged caller escalate into a privileged action.

### Local analog [1](#0-0) 

`Pallet::dispatch_as_fallback_account` is a public, unprivileged, signed extrinsic:

```rust
pub fn dispatch_as_fallback_account(
    mut origin: OriginFor<T>,
    call: Box<<T as Config>::RuntimeCall>,
) -> DispatchResultWithPostInfo {
    Self::ensure_non_contract_if_signed(&origin)?;
    let account_id = origin.as_signer().ok_or(DispatchError::BadOrigin)?;
    let unmapped_account = T::AddressMapper::to_fallback_account_id(
        &T::AddressMapper::to_address(&account_id),
    );
    origin.set_caller_from(RawOrigin::Signed(unmapped_account));
    call.dispatch(origin)
}
```

The identity swap uses `AccountId32Mapper`: [2](#0-1) 

```rust
fn to_address(account_id: &AccountId32) -> H160 {
    ...
    let account_hash = keccak_256(account_bytes);
    H160::from_slice(&account_hash[12..])
}

fn to_fallback_account_id(address: &H160) -> AccountId32 {
    let mut account_id = AccountId32::new([0xEE; 32]);
    let account_bytes: &mut [u8; 32] = account_id.as_mut();
    account_bytes[..20].copy_from_slice(address.as_bytes());
    account_id
}
```

### Finding Description

For any signed `AccountId32`, `dispatch_as_fallback_account` computes a deterministic *derived* account (`hash(account_id)[12..32]` padded with `0xEE`) and then dispatches an **arbitrary caller-supplied `RuntimeCall`** with that derived account as origin — with no restriction on which pallet/call is dispatched, beyond the origin's own call filter, exactly mirroring the `walletAdminCall()` flaw: the wrapper binds the *derived identity* correctly (only the true controller of `account_id` can produce this specific derived account), but places no limit on *which function* is executed as that identity.

The documented purpose is narrow — recovering value that a contract's `terminate`/`send_to_argument` accidentally routed to an un-mapped fallback account, as shown in the test: [3](#0-2) 

However, the fallback `AccountId32` is a completely generic, runtime-wide account — usable and addressable by *every* pallet (Balances, Staking, Proxy, NFTs, Multisig, governance voting, nomination pools, etc.), not only by pallet-revive. Anything that ever credits value, grants a role, or records ownership to that specific 32-byte account — for any reason unrelated to contract termination (a mistyped `balances::transfer`, an XCM teleport/reserve-transfer landing on that address, staking payee/reward routing, a `proxy::add_proxy` delegation, NFT/asset ownership, or governance whitelist entries) — becomes fully claimable by whoever can sign for the original `account_id`, because `call.dispatch(origin)` accepts *any* `RuntimeCall`, not just fund-recovery calls.

### Impact Explanation

This is a public, permissionless origin-escalation primitive: an unprivileged, ordinary user obtains full dispatch rights (subject only to generic call filters) over an account they never held keys for, whenever that account happens to accumulate value or privilege. This can result in theft of misdirected funds, hijacking of proxy/multisig relationships registered against the fallback account, or seizure of any other state (staking positions, NFTs, governance weight) bound to it — satisfying the "theft/unauthorized execution/origin escalation" impact criteria without requiring any privileged actor, admin, or malicious peer.

### Likelihood Explanation

The extrinsic is callable by any signed account with no special permission, and the derivation is a pure deterministic function of the caller's own `AccountId32`, so any attacker can precompute their own fallback account and simply wait for (or induce, e.g. via a mistaken transfer link, misconfigured XCM destination, or dApp bug) value/state to land there, then reclaim it via a single call. Because pallet-revive is deployed on Asset Hub / EVM-compatible parachains where cross-pallet interaction with generic `AccountId32`s is common, the likelihood of incidental value accruing to arbitrary hash-derived accounts is non-trivial.

### Recommendation

Restrict `dispatch_as_fallback_account` to only the narrow set of calls needed for fund recovery (e.g., whitelist `Balances::transfer_all`/similar transfer-out calls) rather than accepting an arbitrary `RuntimeCall`, mirroring the `ProxyAdmin` fix recommendation of whitelisting the specific permitted functions instead of allowing any call under the borrowed identity. Alternatively, require that funds/state be explicitly proven to originate from a pallet-revive contract-termination event before allowing dispatch under the fallback identity.

### Proof of Concept

1. Attacker holds a normal signed `AccountId32` `A`. Compute `addr = to_address(A) = keccak256(A)[12..32]` and `fallback = to_fallback_account_id(addr)` (0xEE-suffixed, `addr` prefixed).
2. Independently (via mistake, misconfigured off-chain tooling, or XCM/teleport routing bug), some unrelated value, proxy delegation, or staking role becomes associated on-chain with the 32-byte account `fallback` (this account is indistinguishable from any ordinary `AccountId32` to other pallets).
3. Attacker calls `Revive::dispatch_as_fallback_account(origin=signed(A), call=<arbitrary RuntimeCall, e.g. Balances::transfer_all, Proxy::proxy, Staking::bond>)`.
4. The pallet sets `origin = Signed(fallback)` and dispatches `call` with `fallback`'s full authority — even though `fallback`'s keys were never controlled by anyone; only knowledge of the preimage `A` (which the attacker trivially possesses, being themselves) is required, as shown in [4](#0-3) .

*Note: I could not find evidence within the indexed codebase of another pallet that explicitly writes state to this fallback-account pattern outside of the contract-termination recovery scenario; the severity therefore depends on whether such incidental value-accrual paths exist elsewhere in the runtime (e.g., XCM beneficiary resolution, mistaken transfers). A full assessment of exploitability at scale would require checking all pallets/XCM configs for any path that can deposit value or grant privilege to an arbitrary/attacker-chosen `AccountId32` matching this derivation.*

### Citations

**File:** substrate/frame/revive/src/lib.rs (L1711-1735)
```rust
		/// Dispatch an `call` with the origin set to the callers fallback address.
		///
		/// Every `AccountId32` can control its corresponding fallback account. The fallback account
		/// is the `AccountId20` with the last 12 bytes set to `0xEE`. This is essentially a
		/// recovery function in case an `AccountId20` was used without creating a mapping first.
		#[pallet::call_index(9)]
		#[pallet::weight({
			let dispatch_info = call.get_dispatch_info();
			(
				<T as Config>::WeightInfo::dispatch_as_fallback_account().saturating_add(dispatch_info.call_weight),
				dispatch_info.class
			)
		})]
		pub fn dispatch_as_fallback_account(
			mut origin: OriginFor<T>,
			call: Box<<T as Config>::RuntimeCall>,
		) -> DispatchResultWithPostInfo {
			Self::ensure_non_contract_if_signed(&origin)?;
			let account_id = origin.as_signer().ok_or(DispatchError::BadOrigin)?;
			let unmapped_account = T::AddressMapper::to_fallback_account_id(
				&T::AddressMapper::to_address(&account_id),
			);
			origin.set_caller_from(RawOrigin::Signed(unmapped_account));
			call.dispatch(origin)
		}
```

**File:** substrate/frame/revive/src/address.rs (L124-147)
```rust
	fn to_address(account_id: &AccountId32) -> H160 {
		let account_bytes: &[u8; 32] = account_id.as_ref();
		if Self::is_eth_derived(account_id) {
			// this was originally an eth address
			// we just strip the 0xEE suffix to get the original address
			H160::from_slice(&account_bytes[..20])
		} else {
			// this is an (ed|sr)25510 derived address
			// avoid truncating the public key by hashing it first
			let account_hash = keccak_256(account_bytes);
			H160::from_slice(&account_hash[12..])
		}
	}

	fn to_account_id(address: &H160) -> AccountId32 {
		<OriginalAccount<T>>::get(address).unwrap_or_else(|| Self::to_fallback_account_id(address))
	}

	fn to_fallback_account_id(address: &H160) -> AccountId32 {
		let mut account_id = AccountId32::new([0xEE; 32]);
		let account_bytes: &mut [u8; 32] = account_id.as_mut();
		account_bytes[..20].copy_from_slice(address.as_bytes());
		account_id
	}
```

**File:** substrate/frame/revive/src/tests/pvm.rs (L4022-4057)
```rust
#[test]
fn recovery_works() {
	let (code, _) = compile_module("terminate_and_send_to_argument").unwrap();

	ExtBuilder::default().existential_deposit(100).build().execute_with(|| {
		<Test as Config>::Currency::set_balance(&ALICE, 1_000_000);

		let initial_contract_balance = 100_000;

		// eve puts her AccountId20 as argument to terminate but forgot to register
		// her AccountId32 first so now the funds are trapped in her fallback account
		let Contract { addr, .. } = builder::bare_instantiate(Code::Upload(code.clone()))
			.native_value(initial_contract_balance)
			.build_and_unwrap_contract();
		assert_eq!(<Test as Config>::Currency::total_balance(&EVE), 0);
		assert_eq!(<Test as Config>::Currency::total_balance(&EVE_FALLBACK), 0);
		builder::bare_call(addr).data(EVE_ADDR.encode()).build_and_unwrap_result();
		assert_eq!(
			<Test as Config>::Currency::total_balance(&EVE_FALLBACK),
			initial_contract_balance + 100,
		);
		assert_eq!(<Test as Config>::Currency::total_balance(&EVE), 0);

		let call = RuntimeCall::Balances(pallet_balances::Call::transfer_all {
			dest: EVE,
			keep_alive: false,
		});

		// she now uses the recovery function to move all funds from the fallback
		// account to her real account
		<Pallet<Test>>::dispatch_as_fallback_account(RuntimeOrigin::signed(EVE), Box::new(call))
			.unwrap();
		assert_eq!(<Test as Config>::Currency::total_balance(&EVE_FALLBACK), 0);
		assert_eq!(<Test as Config>::Currency::total_balance(&EVE), initial_contract_balance + 100);
	});
}
```
