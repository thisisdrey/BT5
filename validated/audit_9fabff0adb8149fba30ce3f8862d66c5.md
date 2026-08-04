### Title
Front-runnable account binding in `set_alias_account` / `set_personal_id_account` lets an unprivileged person permanently lock a target account, causing DoS of legitimate account setup - (File: `substrate/frame/people/src/lib.rs`)

### Summary
Both `Pallet::set_alias_account` and `Pallet::set_personal_id_account` bind an arbitrary, caller-supplied `account: T::AccountId` to the caller's personhood proof, guarded only by a "not already in use" check on public storage maps (`AccountToPersonalId`, `AccountToAlias`). Because the target `account` is never proven to be controlled by the caller and the only defense against squatting is "first write wins," any account that has already proven personhood once (a "person" in the ring, not a validator/collator/admin) can observe a pending legitimate transaction (or simply guess/target a well-known address) and submit its own call naming the same `account` first. This mirrors the reported `NarwhalReferrals.signUp` bug: a first-come-first-served mapping assignment with no binding to the true intended party, enabling front-run squatting and permanent denial of service.

### Finding Description
`set_alias_account` takes a caller-chosen `account` and, after checking the account isn't already used, unconditionally does `AccountToAlias::<T>::insert(&account, &rev_ca)` / `AliasToAccount::<T>::insert(&rev_ca.ca, &account)`: [1](#0-0) 

The only pre-conditions checked are timing (`call_valid_at`/`time_tolerance`), context validity, and that `account` is not already mapped in `AccountToPersonalId` or `AccountToAlias`: [2](#0-1) [3](#0-2) 

Nothing proves that the extrinsic sender actually controls `account` — the origin only proves personhood (`ensure_revised_personal_alias`), not ownership of the target `AccountId`. The identical pattern exists in `set_personal_id_account`, which similarly checks only "not in use" before writing `AccountToPersonalId::<T>::insert(&account, id)`: [4](#0-3) 

Since these calls are ordinary signed extrinsics, they sit in the public mempool before inclusion. Any already-onboarded person can see a pending `set_alias_account(account = X, ...)` (or simply target an address they expect a victim to want, e.g. a fresh/known account) and submit their own `set_alias_account(account = X, ...)` with higher priority/gas or in the same block earlier in extrinsic order. Once `AccountToAlias`/`AccountToPersonalId` contains `X` pointing at the attacker's ring, the victim's later call to bind the same `account` fails with `Error::AccountInUse`, permanently blocking the legitimate binding for that address until the attacker voluntarily calls `unset_alias_account`/`unset_personal_id_account`.

This is directly analogous to the reported issue: the referral contract assigned `refLinkToUser[block.number]` on a first-write basis with no cryptographic tie to the legitimate caller, letting an attacker front-run and squat the slot to deny the intended user. Here, the "slot" is an arbitrary `AccountId` bound to a personhood/alias record on a first-write basis, with no proof that the caller controls that account.

### Impact Explanation
An unprivileged, already-onboarded person can grief any other person's account-setup flow by squatting the specific account address they intend to use as their alias/personal-id account. This is a public-dispatch-wrapper style denial-of-service against the People pallet's core "link account to personhood" functionality with no admin/validator/collator/relayer involvement — it degrades intended chain behavior (permanent inability to set up an account binding for an affected address) rather than merely front-running one transaction's outcome.

### Likelihood Explanation
Likelihood is moderate-to-high in any deployment where account/alias setup transactions are visible pre-inclusion (standard mempool behavior) and where becoming "a person" (satisfying `ensure_revised_personal_alias`/`ensure_personal_identity`) is the only prerequisite — i.e., any already-recognized person, not a privileged actor, can perform the attack repeatedly and cheaply (each call costs at most the extrinsic fee, refunded on success per the doc comments).

### Recommendation
Do not allow the caller to name an arbitrary target `AccountId` for binding based purely on "not currently in use." Require the `account` parameter to independently prove control (e.g., a signature from `account` itself co-signed into the same extrinsic/proof, or restrict `set_alias_account`/`set_personal_id_account` to `ensure_signed` where the signed origin *is* the account being bound) so that a proof-of-personhood origin cannot unilaterally claim a third-party's `AccountId`.

### Proof of Concept
1. Attacker Bob has already proven personhood (`ensure_revised_personal_alias`/`ensure_personal_identity` succeeds for him).
2. Victim Alice broadcasts `set_alias_account(account = X, call_valid_at = N)` intending to bind her wallet `X` to her alias.
3. Bob observes Alice's pending transaction in the mempool (or predicts `X` in advance) and submits `set_alias_account(account = X, call_valid_at = N)` from his own ring/context with higher tip or earlier position in the block.
4. Bob's transaction executes first: `AccountToAlias::<T>::insert(X, bob_rev_ca)` succeeds since `X` was unused. [5](#0-4) 
5. Alice's transaction then executes and fails with `Error::<T>::AccountInUse` at the check `ensure!(!AccountToAlias::<T>::contains_key(&account), Error::<T>::AccountInUse);`. [6](#0-5) 
6. Alice can never bind account `X` unless Bob calls `unset_alias_account`, achieving a targeted, permanent denial of service on her intended account-setup flow.

### Citations

**File:** substrate/frame/people/src/lib.rs (L912-954)
```rust
		pub fn set_alias_account(
			origin: OriginFor<T>,
			account: T::AccountId,
			call_valid_at: BlockNumberFor<T>,
		) -> DispatchResultWithPostInfo {
			let rev_ca = Self::ensure_revised_personal_alias(origin)?;
			let now = frame_system::Pallet::<T>::block_number();
			let time_tolerance = Self::account_setup_time_tolerance();
			ensure!(
				call_valid_at <= now && now <= call_valid_at.saturating_add(time_tolerance),
				Error::<T>::TimeOutOfRange
			);
			ensure!(T::AccountContexts::contains(&rev_ca.ca.context), Error::<T>::InvalidContext);
			ensure!(!AccountToPersonalId::<T>::contains_key(&account), Error::<T>::AccountInUse);

			let old_account = AliasToAccount::<T>::get(&rev_ca.ca);
			let old_rev_ca = old_account.as_ref().and_then(AccountToAlias::<T>::get);

			let needs_revision = old_rev_ca.is_some_and(|old_rev_ca| {
				old_rev_ca.revision != rev_ca.revision || old_rev_ca.ring != rev_ca.ring
			});

			// Ensure it changes the account associated, or it needs revision.
			ensure!(
				old_account.as_ref() != Some(&account) || needs_revision,
				Error::<T>::AliasAccountAlreadySet
			);

			// If the old account is different from the new one:
			// * decrease the sufficients of the old account
			// * increase the sufficients of the new account
			// * check new account is not already in use
			if old_account.as_ref() != Some(&account) {
				ensure!(!AccountToAlias::<T>::contains_key(&account), Error::<T>::AccountInUse);
				if let Some(old_account) = &old_account {
					frame_system::Pallet::<T>::dec_sufficients(old_account);
					AccountToAlias::<T>::remove(old_account);
				}
				frame_system::Pallet::<T>::inc_sufficients(&account);
			}

			AccountToAlias::<T>::insert(&account, &rev_ca);
			AliasToAccount::<T>::insert(&rev_ca.ca, &account);
```

**File:** substrate/frame/people/src/lib.rs (L1009-1034)
```rust
		pub fn set_personal_id_account(
			origin: OriginFor<T>,
			account: T::AccountId,
			call_valid_at: BlockNumberFor<T>,
		) -> DispatchResultWithPostInfo {
			let id = Self::ensure_personal_identity(origin)?;
			let now = frame_system::Pallet::<T>::block_number();
			let time_tolerance = Self::account_setup_time_tolerance();
			ensure!(
				call_valid_at <= now && now <= call_valid_at.saturating_add(time_tolerance),
				Error::<T>::TimeOutOfRange
			);
			ensure!(!AccountToPersonalId::<T>::contains_key(&account), Error::<T>::AccountInUse);
			ensure!(!AccountToAlias::<T>::contains_key(&account), Error::<T>::AccountInUse);
			let mut record = People::<T>::get(id).ok_or(Error::<T>::NotPerson)?;
			let pays = if let Some(old_account) = record.account {
				frame_system::Pallet::<T>::dec_sufficients(&old_account);
				AccountToPersonalId::<T>::remove(&old_account);
				Pays::Yes
			} else {
				Pays::No
			};
			record.account = Some(account.clone());
			frame_system::Pallet::<T>::inc_sufficients(&account);
			AccountToPersonalId::<T>::insert(&account, id);
			People::<T>::insert(id, &record);
```
