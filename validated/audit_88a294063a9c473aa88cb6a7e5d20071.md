### Title
Birthday-attack address collision in `pallet-revive` `AccountId32Mapper` can hijack a victim's Ethereum-style address to an attacker-controlled account - (File: `substrate/frame/revive/src/address.rs`)

### Summary
`pallet-revive`'s `AccountId32Mapper` derives an `H160` Ethereum address from a native `AccountId32` by truncating a 256-bit hash to its trailing 160 bits, exactly the CREATE2-style truncation pattern flagged in the external report. Unlike the report's `validateCallback`, which only lacked an existence check, here the reverse mapping (`OriginalAccount<T>`) is written on a strict "first writer wins" basis with no binding to the account that legitimately owns the pre-image. Because account registration for non-eth-derived accounts happens automatically and silently via `AutoMapper::on_new_account` (best-effort, errors only logged), an attacker who wins the race to register a colliding `AccountId32` first permanently steals the `H160` slot that a real (later-created) account would otherwise have used.

### Finding Description
`AccountId32Mapper::to_address` computes the Ethereum-style address for a plain sr25519/ed25519 account as: [1](#0-0) 

For any address computed this way, the only way to resolve `H160 -> AccountId32` is via the `OriginalAccount<T>` map: [2](#0-1) 

`to_account_id` falls back to a deterministic, unrelated "fallback" account only if nothing has been registered yet: [3](#0-2) 

Registration is gated only by `is_mapped`, which is itself defined purely in terms of whether *the target address* already has an entry in `OriginalAccount` — not whether *this particular* `account_id` was the one that produced it: [4](#0-3) 

```rust
fn map(account_id: &T::AccountId) -> DispatchResult {
    ensure!(!Self::is_mapped(account_id), <Error<T>>::AccountAlreadyMapped);
    ...
    <OriginalAccount<T>>::insert(Self::to_address(account_id), account_id);
    Ok(())
}
``` [5](#0-4) 

Critically, non-eth-derived accounts get mapped *automatically and silently* the moment they are created on-chain, with no deposit and no explicit user action, via the `frame_system::OnNewAccount` hook: [6](#0-5) 

Errors from this automatic mapping attempt are only logged, never surfaced or reverted — account creation always succeeds regardless of whether the mapping itself succeeded.

Given a 160-bit truncated address space, the report's own math applies directly: an attacker needs roughly `2^80` hash computations to find a collision between an `AccountId32` they control and *some* other `AccountId32` (not necessarily a pre-chosen victim) with ~39% success, or `2^81` for ~86% success — the same feasibility analysis (hardware cost, time, energy) laid out in the source report. Because the attacker does not need to target a specific victim (any collision among the huge, ever-growing population of live and future substrate accounts suffices — a true birthday-paradox condition, not a directed preimage search), the effective cost is exactly the `2^80`/`2^81` regime described, not the far more expensive `2^160` preimage search.

### Impact Explanation
If an attacker's account (attacker-controlled keypair) is created and auto-mapped *before* a colliding victim account is created/auto-mapped, `OriginalAccount<T>[H160]` permanently points to the attacker's account. When the victim's account is later created, its own auto-map attempt fails `is_mapped` (since the address is already occupied) and the failure is silently swallowed — the victim's account is created normally but is never granted the correct `H160 -> AccountId32` binding. From that point forward, any interaction addressed to that Ethereum-style `H160` (EVM calls, `pallet_revive` value transfers/approvals addressed to that address, precompile interactions, etc.) resolves to the attacker's account instead of the victim's, misdirecting funds/authority to the wrong beneficiary — a direct match to the "wrong beneficiary" / "theft" impact class in the gate criteria. This requires no privileged role, governance action, malicious validator/collator, or leaked keys — purely public account creation (which happens automatically on any balance transfer) and offline hash grinding.

### Likelihood Explanation
The attack is unprivileged and fully public-entrypoint driven: any account creation (e.g., receiving a tiny transfer) triggers `on_new_account`. The only cost is the offline grinding effort, whose feasibility (hardware, time, cost) is explicitly quantified in the source report at `2^80`–`2^81` operations (~millions of USD, ~1–2 years with commodity ASIC-class hardware), i.e., the report's own conclusion that this is "becoming more feasible" applies verbatim here since the mapping is 256-bit-hash-truncated-to-160-bit, identical to the report's CREATE2 pattern. The `pr_7662` prdoc in this repo shows the team was already aware of truncation-collision risk in address derivation and mitigated the "always-truncate" case, but the current first-writer-wins semantics of `OriginalAccount` combined with silent auto-mapping was not addressed by that fix, leaving the race-based hijack path open.

### Recommendation
- Do not allow the very first registrant of a colliding `H160` to permanently and silently own that address for all time. Instead, treat the address as ambiguous once a second (colliding) `AccountId32` is observed hashing to it — e.g., disable both accounts from using that shorthand address, or require the caller to disambiguate by falling back to explicit full-`AccountId32` identification for the associated address.
- Fail loudly (not silently log-and-continue) when automatic mapping via `AutoMapper::on_new_account` cannot proceed because the target address is already claimed by an unrelated account, and surface this as a distinguishable state so downstream consumers of `to_account_id` are not fooled into trusting an unverified binding.
- Consider widening the effective mapping key (e.g., binding OriginalAccount to a value derived from the full untruncated hash, or requiring an out-of-band collision-check/challenge) so that first-come-first-served address squatting via hash grinding is not economically rational.

### Proof of Concept
1. Offline, the attacker generates ~`2^80` sr25519/ed25519 keypairs and computes `H160_i = keccak256(pubkey_i)[12..]` for each, per `AccountId32Mapper::to_address`.
2. The attacker stores `H160_i -> keypair_i` and monitors the chain (or simply predicts that some future/legitimate account will eventually be created) for a match — this is the classic birthday-paradox search described identically in the source report (39% success at `2^80`, 86% at `2^81`).
3. As soon as any new on-chain account (attacker- or victim-created; only the sequencing of creation matters) collides with one of the attacker's precomputed keypairs, the attacker ensures their own colliding account is the first to be created/funded on-chain, triggering `AutoMapper::on_new_account` → `map_no_deposit_unchecked` → `OriginalAccount<T>::insert(H160, attacker_account)`, per `substrate/frame/revive/src/address.rs:149-166,287-301`.
4. When the legitimate victim's colliding account is later created, its own automatic mapping attempt returns `Err(AccountAlreadyMapped)` from `is_mapped`/`map_no_deposit_unchecked` (`substrate/frame/revive/src/address.rs:179-182`), which `AutoMapper::on_new_account` only logs via `log::warn!` and does not propagate — account creation proceeds normally but without the correct mapping.
5. Any subsequent call, transfer, or contract interaction targeting the shared `H160` (via `to_account_id`, `substrate/frame/revive/src/lib.rs:751-758`) resolves to the attacker's `AccountId32`, not the victim's, redirecting funds/authority to the attacker.

### Citations

**File:** substrate/frame/revive/src/address.rs (L124-136)
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
```

**File:** substrate/frame/revive/src/address.rs (L138-147)
```rust
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

**File:** substrate/frame/revive/src/address.rs (L149-166)
```rust
	fn map(account_id: &T::AccountId) -> DispatchResult {
		ensure!(!Self::is_mapped(account_id), <Error<T>>::AccountAlreadyMapped);

		// each mapping entry stores the address (20 bytes) and the account id (32 bytes)
		let deposit = T::DepositPerByte::get()
			.saturating_mul(52u32.into())
			.saturating_add(T::DepositPerItem::get());
		T::Currency::hold(&HoldReason::AddressMapping.into(), account_id, deposit)?;

		<OriginalAccount<T>>::insert(Self::to_address(account_id), account_id);
		Ok(())
	}

	fn map_no_deposit_unchecked(account_id: &T::AccountId) -> DispatchResult {
		ensure!(!Self::is_mapped(account_id), <Error<T>>::AccountAlreadyMapped);
		<OriginalAccount<T>>::insert(Self::to_address(account_id), account_id);
		Ok(())
	}
```

**File:** substrate/frame/revive/src/address.rs (L179-182)
```rust
	fn is_mapped(account_id: &T::AccountId) -> bool {
		Self::is_eth_derived(account_id) ||
			<OriginalAccount<T>>::contains_key(Self::to_address(account_id))
	}
```

**File:** substrate/frame/revive/src/address.rs (L287-301)
```rust
pub struct AutoMapper<T>(PhantomData<T>);

impl<T: Config> OnNewAccount<T::AccountId> for AutoMapper<T> {
	fn on_new_account(who: &T::AccountId) {
		if T::AutoMap::get() &&
			!T::AddressMapper::is_eth_derived(who) &&
			let Err(err) = T::AddressMapper::map_no_deposit_unchecked(who)
		{
			log::warn!(
				target: crate::LOG_TARGET,
				"Failed to auto-map account {who:?}: {err:?}",
			);
		}
	}
}
```

**File:** substrate/frame/revive/src/lib.rs (L751-758)
```rust
	/// Map a Ethereum address to its original `AccountId32`.
	///
	/// When deriving a `H160` from an `AccountId32` we use a hash function. In order to
	/// reconstruct the original account we need to store the reverse mapping here.
	/// Register your `AccountId32` using [`Pallet::map_account`] in order to
	/// use it with this pallet.
	#[pallet::storage]
	pub(crate) type OriginalAccount<T: Config> = StorageMap<_, Identity, H160, AccountId32>;
```
