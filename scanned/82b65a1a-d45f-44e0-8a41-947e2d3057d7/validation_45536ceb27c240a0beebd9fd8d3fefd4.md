### Title
Unauthorized Mapping Manipulation in `OriginalAccount` - ([File: substrate/frame/revive/src/address.rs])

### Summary
The `pallet-revive` uses an `OriginalAccount` storage map to reverse-map Ethereum `H160` addresses to Substrate `AccountId32` accounts. This mapping is critical for origin authentication. A vulnerability exists where unprivileged users can manipulate these mappings for accounts they do not control through `batch_map_accounts`, or where the lack of strict binding between the mapping and the account's lifecycle allows for state inconsistencies that affect origin resolution.

### Finding Description
In `pallet-revive`, the `AddressMapper` trait implementation for `AccountId32Mapper` manages the `OriginalAccount` storage map [1](#0-0) . This map is used by `to_account_id` to determine which Substrate account is the "true" owner of an Ethereum address [2](#0-1) .

The `batch_map_accounts` dispatchable allows any signed origin to register mappings for a list of `accounts` [3](#0-2) . While it filters for accounts that exist in `frame_system` [4](#0-3) , it does not verify that the caller owns these accounts or has permission to map them. It uses `map_no_deposit_unchecked`, which inserts the mapping into storage without taking the standard security deposit [5](#0-4) .

This allows an attacker to:
1.  **State Bloat/Griefing**: Permanently occupy `OriginalAccount` slots for arbitrary existing accounts without paying the required deposit [6](#0-5) .
2.  **Origin Ambiguity**: If an account is mapped via `batch_map_accounts` by a third party, the owner of that account loses the ability to call `map_account` themselves (which would fail with `AccountAlreadyMapped`) [7](#0-6) , potentially interfering with workflows that expect the owner to have initiated the mapping and paid the deposit.

### Impact Explanation
The `OriginalAccount` mapping is the root of trust for origin escalation in `pallet-revive`. If an attacker can manipulate when and how an account is mapped, they can cause public underpriced work (bypassing deposits) and degrade the chain's state by filling it with unbacked `OriginalAccount` entries. Because `to_account_id` relies on this map to resolve the caller of Ethereum transactions, any corruption or unauthorized entry in this map directly affects the integrity of the pallet's access control.

### Likelihood Explanation
The likelihood is high because `batch_map_accounts` is a public dispatchable and the check for account existence in `frame_system` is insufficient to prevent unauthorized mapping of other users' accounts. The economic incentive to bypass deposits or grief other users by mapping their accounts is present in public permissionless environments.

### Recommendation
Restrict `batch_map_accounts` to only allow mapping accounts where the caller has proven authority, or ensure that the mapping can only be initiated by the account owner. Alternatively, enforce that even in batch operations, the appropriate deposit is held from the target account's balance to maintain the economic invariant.

### Proof of Concept
1.  Attacker identifies a set of active `AccountId32` addresses on-chain that have not yet registered with `pallet-revive`.
2.  Attacker calls `batch_map_accounts(origin, accounts)` providing these addresses.
3.  The pallet verifies the accounts exist in `frame_system::Account` [4](#0-3) .
4.  The pallet calls `map_no_deposit_unchecked`, which inserts the mappings into `OriginalAccount` [8](#0-7) .
5.  The attacker pays only the transaction fee (or none if 90% are "useful"), while the `OriginalAccount` storage is now permanently populated for these users without any deposit being held from them or the attacker [9](#0-8) .

### Citations

**File:** substrate/frame/revive/src/address.rs (L120-124)
```rust
impl<T> AddressMapper<T> for AccountId32Mapper<T>
where
	T: Config<AccountId = AccountId32>,
{
	fn to_address(account_id: &AccountId32) -> H160 {
```

**File:** substrate/frame/revive/src/address.rs (L138-140)
```rust
	fn to_account_id(address: &H160) -> AccountId32 {
		<OriginalAccount<T>>::get(address).unwrap_or_else(|| Self::to_fallback_account_id(address))
	}
```

**File:** substrate/frame/revive/src/address.rs (L150-150)
```rust
		ensure!(!Self::is_mapped(account_id), <Error<T>>::AccountAlreadyMapped);
```

**File:** substrate/frame/revive/src/address.rs (L162-166)
```rust
	fn map_no_deposit_unchecked(account_id: &T::AccountId) -> DispatchResult {
		ensure!(!Self::is_mapped(account_id), <Error<T>>::AccountAlreadyMapped);
		<OriginalAccount<T>>::insert(Self::to_address(account_id), account_id);
		Ok(())
	}
```

**File:** substrate/frame/revive/src/lib.rs (L1630-1635)
```rust
		pub fn batch_map_accounts(
			origin: OriginFor<T>,
			accounts: Vec<T::AccountId>,
		) -> DispatchResultWithPostInfo {
			ensure_signed(origin.clone())?;
			Self::ensure_non_contract_if_signed(&origin)?;
```

**File:** substrate/frame/revive/src/lib.rs (L1646-1646)
```rust
				.filter(|&a| frame_system::Pallet::<T>::account_exists(a))
```

**File:** substrate/frame/revive/src/lib.rs (L1650-1653)
```rust
				match T::AddressMapper::map_no_deposit_unchecked(account_id) {
					Ok(()) => {
						useful = true;
					},
```

**File:** substrate/frame/revive/src/lib.rs (L1688-1692)
```rust
			if proportion_mapped >= Perbill::from_percent(90) {
				Ok(Pays::No.into())
			} else {
				Ok(Pays::Yes.into())
			}
```
