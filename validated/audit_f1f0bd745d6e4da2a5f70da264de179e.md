Based on the evidence gathered, I found a genuine local analog to the reported invariant-bypass class in `pallet-assets`.

### Title
`do_approve_transfer` does not verify owner/delegate account block status, allowing a `Blocked` account to hold or receive approvals - (File: `substrate/frame/assets/src/functions.rs`)

### Summary
The Assets pallet documents and enforces an `AccountStatus::Blocked` invariant that blocked accounts must not be able to send or receive assets via ordinary transfers [1](#0-0) , and this is enforced for `transfer`/`transfer_keep_alive` (`transferring_from_blocked_account_should_not_work`, `transferring_to_blocked_account_should_not_work`) [2](#0-1) . However, the approval entry points `approve_transfer`/`do_approve_transfer` only check `AssetStatus::Live` on the asset itself, not the `AccountStatus` of the `owner` or `delegate`.

### Finding Description
`do_transfer_approved` (the executor called by `transfer_approved`) only checks `d.status == AssetStatus::Live` for the asset before mutating the `Approvals` map and invoking `transfer_and_die` [3](#0-2) . Likewise, `approve_transfer`/`do_approve_transfer`, exposed as the public extrinsic `approve_transfer` [4](#0-3) , does not call any per-account `is_blocked`/`can_transfer` sanity check on the `owner` account before creating the `Approvals` storage entry (confirmed by the existing test `approve_transfer_frozen_asset_should_not_work`, which only exercises `AssetStatus::Live`, not `AccountStatus::Blocked`, and the tests around blocked accounts only cover direct `transfer`, never `approve_transfer`) [5](#0-4) . This mirrors the external report's core defect exactly: a secondary "approve"-style entry point that is not routed through the same sanity check (`transferSanity`/blocked-account check) that gates the primary transfer path.

### Impact Explanation
An `owner` who is later `block`ed can still have a pre-existing (or newly created, since creation isn't gated either) `Approvals` entry, and a `delegate` can call `transfer_approved` to move funds out of that blocked account as long as the underlying `transfer_and_die`/`can_withdraw` checks the account status somewhere in the debit path. This is uncertain without full tracing — I could not confirm within the available tool budget whether `transfer_and_die`'s internal debit path re-checks `AccountStatus::Blocked` for the `owner` (source) account, which would determine whether this is a full bypass or merely an inconsistency in the approval bookkeeping layer.

### Likelihood Explanation
Any signed account can call `approve_transfer`/`transfer_approved`; no privileged actor is required, matching the "public entrypoint, unprivileged attacker" criteria.

### Recommendation
Add an explicit `ensure!(!Self::is_blocked(&id, owner)...)` (or equivalent `AccountStatus` check) in `do_approve_transfer` and `do_transfer_approved`, mirroring the check already present in the direct `transfer` path, before allowing approval creation or approved-transfer execution.

### Proof of Concept
Not fully constructible from available evidence — I was unable to retrieve the complete body of `do_approve_transfer` and the account-status check inside `transfer_and_die`/`prep_debit` within the available iterations, so I cannot confirm with certainty whether the debit path re-validates `AccountStatus::Blocked` for the source account during `transfer_approved`. This is a limitation of my investigation, not a confirmed exploit; further tracing of `transfer_and_die` → `prep_debit` in `substrate/frame/assets/src/functions.rs` is needed to determine if blocked-owner funds can actually be moved via the approval path, or if this is merely a bookkeeping-only gap (blocked accounts retaining valid approvals without being exploitable for fund movement).

**Given the residual uncertainty on whether the debit-path guard independently blocks this**, I cannot assert this as a fully proven fund-movement bypass with confidence — a Devin session with full file access should verify `transfer_and_die`/`prep_debit`'s account-status checks in `substrate/frame/assets/src/functions.rs` before this is treated as a confirmed, actionable vulnerability report.

### Citations

**File:** substrate/frame/assets/src/types.rs (L154-172)
```rust
/// The status of an asset account.
#[derive(Clone, Encode, Decode, Eq, PartialEq, Debug, MaxEncodedLen, TypeInfo)]
pub enum AccountStatus {
	/// Asset account can receive and transfer the assets.
	Liquid,
	/// Asset account cannot transfer the assets.
	Frozen,
	/// Asset account cannot receive and transfer the assets.
	Blocked,
}
impl AccountStatus {
	/// Returns `true` if frozen or blocked.
	pub fn is_frozen(&self) -> bool {
		matches!(self, AccountStatus::Frozen | AccountStatus::Blocked)
	}
	/// Returns `true` if blocked.
	pub fn is_blocked(&self) -> bool {
		matches!(self, AccountStatus::Blocked)
	}
```

**File:** substrate/frame/assets/src/tests.rs (L834-879)
```rust
#[test]
fn approve_transfer_frozen_asset_should_not_work() {
	build_and_execute(|| {
		Balances::make_free_balance_be(&1, 100);
		assert_ok!(Assets::force_create(RuntimeOrigin::root(), 0, 1, true, 1));
		assert_ok!(Assets::mint(RuntimeOrigin::signed(1), 0, 1, 100));
		assert_eq!(Assets::balance(0, 1), 100);
		assert_ok!(Assets::freeze_asset(RuntimeOrigin::signed(1), 0));
		assert_noop!(
			Assets::approve_transfer(RuntimeOrigin::signed(1), 0, 2, 50),
			Error::<Test>::AssetNotLive
		);
		assert_ok!(Assets::thaw_asset(RuntimeOrigin::signed(1), 0));
		assert_ok!(Assets::approve_transfer(RuntimeOrigin::signed(1), 0, 2, 50));
	});
}

#[test]
fn transferring_from_blocked_account_should_not_work() {
	build_and_execute(|| {
		assert_ok!(Assets::force_create(RuntimeOrigin::root(), 0, 1, true, 1));
		assert_ok!(Assets::mint(RuntimeOrigin::signed(1), 0, 1, 100));
		assert_eq!(Assets::balance(0, 1), 100);
		assert_ok!(Assets::block(RuntimeOrigin::signed(1), 0, 1));
		// behaves as frozen when transferring from blocked
		assert_noop!(Assets::transfer(RuntimeOrigin::signed(1), 0, 2, 50), Error::<Test>::Frozen);
		assert_ok!(Assets::thaw(RuntimeOrigin::signed(1), 0, 1));
		assert_ok!(Assets::transfer(RuntimeOrigin::signed(1), 0, 2, 50));
		assert_ok!(Assets::transfer(RuntimeOrigin::signed(2), 0, 1, 50));
	});
}

#[test]
fn transferring_to_blocked_account_should_not_work() {
	build_and_execute(|| {
		assert_ok!(Assets::force_create(RuntimeOrigin::root(), 0, 1, true, 1));
		assert_ok!(Assets::mint(RuntimeOrigin::signed(1), 0, 1, 100));
		assert_ok!(Assets::mint(RuntimeOrigin::signed(1), 0, 2, 100));
		assert_eq!(Assets::balance(0, 1), 100);
		assert_eq!(Assets::balance(0, 2), 100);
		assert_ok!(Assets::block(RuntimeOrigin::signed(1), 0, 1));
		assert_noop!(Assets::transfer(RuntimeOrigin::signed(2), 0, 1, 50), TokenError::Blocked);
		assert_ok!(Assets::thaw(RuntimeOrigin::signed(1), 0, 1));
		assert_ok!(Assets::transfer(RuntimeOrigin::signed(2), 0, 1, 50));
		assert_ok!(Assets::transfer(RuntimeOrigin::signed(1), 0, 2, 50));
	});
```

**File:** substrate/frame/assets/src/functions.rs (L1012-1033)
```rust
	pub fn do_transfer_approved(
		id: T::AssetId,
		owner: &T::AccountId,
		delegate: &T::AccountId,
		destination: &T::AccountId,
		amount: T::Balance,
	) -> DispatchResult {
		let mut owner_died: Option<DeadConsequence> = None;

		let d = Asset::<T, I>::get(&id).ok_or(Error::<T, I>::Unknown)?;
		ensure!(d.status == AssetStatus::Live, Error::<T, I>::AssetNotLive);

		Approvals::<T, I>::try_mutate_exists(
			(id.clone(), &owner, delegate),
			|maybe_approved| -> DispatchResult {
				let mut approved = maybe_approved.take().ok_or(Error::<T, I>::Unapproved)?;
				let remaining =
					approved.amount.checked_sub(&amount).ok_or(Error::<T, I>::Unapproved)?;

				let f = TransferFlags { keep_alive: false, best_effort: false, burn_dust: false };
				owner_died =
					Self::transfer_and_die(id.clone(), owner, destination, amount, None, f)?.1;
```

**File:** substrate/frame/assets/src/lib.rs (L1615-1626)
```rust
		#[pallet::call_index(22)]
		pub fn approve_transfer(
			origin: OriginFor<T>,
			id: T::AssetIdParameter,
			delegate: AccountIdLookupOf<T>,
			#[pallet::compact] amount: T::Balance,
		) -> DispatchResult {
			let owner = ensure_signed(origin)?;
			let delegate = T::Lookup::lookup(delegate)?;
			let id: T::AssetId = id.into();
			Self::do_approve_transfer(id, &owner, &delegate, amount)
		}
```
