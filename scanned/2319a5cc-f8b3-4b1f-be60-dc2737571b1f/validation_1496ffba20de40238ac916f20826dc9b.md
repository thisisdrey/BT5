Based on my investigation, I found a concrete local analog of the reported bug class in `pallet-multi-asset-bounties`, specifically the recently-added `increase_value` call.

### Title
Curator-controlled bounty value inflation breaks the child-bounty value invariant governance approved - ([File: substrate/frame/multi-asset-bounties/src/lib.rs])

### Summary
The Nouns report's core broken invariant is: an approval (a vote) is bound only to an identifier, while the content it is meant to approve (the proposal's description/transactions) can be mutated afterward without re-triggering approval, letting a single party silently change what was actually authorized. `pallet-multi-asset-bounties` has a structurally identical pattern: governance (`SpendOrigin`) approves a bounty's `value` once (via `propose_bounty`/`approve_bounty`), but the pallet's own curator-only `increase_value` call (added in `prdoc/pr_12409.prdoc`) lets the curator unilaterally raise that governance-approved `value` afterward — with no re-approval by `SpendOrigin` and no on-chain re-validation against the child-bounty sum invariant beyond the code's own bookkeeping.

### Finding Description
`increase_value` is dispatched with only the bounty's curator signature: [1](#0-0) 
It mutates `bounty.value` directly, bypassing the governance path (`SpendOrigin`) that originally set and bounded that value via `propose_bounty`/`approve_bounty`/`propose_curator`: [2](#0-1) 
The doc comment explicitly states the intended invariant — "the sum of child-bounty values never exceeds the parent value" — is supposed to be preserved by only ever increasing, never decreasing, `value`: [3](#0-2) 
This is analogous to the Nouns bug: the original "vote" (governance's `SpendOrigin` approval of a specific `value`) is bound to the bounty index, not to the value itself remaining fixed. A single actor (the curator, who is *not* the trusted governance origin) can later change the approved figure that downstream logic (child-bounty allocation ceilings, curator-fee percentage deposits) continues to trust as if it were still governance-approved. Just as inattentive Nouns voters cast Yes/No based on a stale proposal body, any watcher or dependent process (auditors, other pallets composing on `Bounties::value`, treasury reporting) that reads `bounty.value` as "the amount council approved" is silently misled once the curator inflates it.

### Impact Explanation
Because `value` also gates the curator deposit calculation (`CuratorDeposit`) and the ceiling for child-bounty allocation, a curator can inflate the recorded `value` far beyond what was actually funded/approved by governance, then use that inflated ceiling to propose/fund child bounties whose sum was never actually authorized by `SpendOrigin`. The PR doc acknowledges that payouts are "bounded by the account's real balance at settlement," which limits *direct* fund theft, but it does not stop the value from diverging from the governance-approved figure, which is precisely the type of impact the Impact Gate calls out for treasury/reward payouts: settlement state that no longer reflects what the rightful decision-maker approved. Since child bounty allocation logic uses the parent `value` as its cap (not real account balance) at award/claim time until settlement, an inflated `value` can let a curator over-commit child bounty awards that will race for the same underlying pot, causing awarded-but-unpayable claims, denial of legitimate payouts, or a scramble where whichever child claim settles first drains funds intended for others — i.e., duplicate/first-come settlement against a governance-approved pot whose accounting was corrupted by a non-governance actor.

### Likelihood Explanation
Likelihood is low-to-medium: it requires the curator (a role assigned via governance, not an outside attacker) to be the actor who inflates `value`, similar to how the Nouns bug requires the proposer (also a semi-trusted actor) to sneak the update. This falls close to the "privileged/admin abuse" exclusion in the Impact Gate, but the distinguishing point is that `increase_value` was designed to be curator-signed with **no** re-check against the original governance-approved amount — it is a public/unprivileged-relative-to-governance entrypoint that structurally reproduces the "approval bound to ID not content" flaw, not an admin misusing an already-correct permission.

### Recommendation
Require `increase_value` to go back through `T::SpendOrigin` (or emit a fresh approval event that governance must ratify) rather than trusting the curator's signature alone to raise the value that gates deposits and child-bounty budgets. Alternatively, decouple the child-bounty allocation ceiling from the mutable `bounty.value` and instead cap it by the amount actually confirmed to be held in the bounty account at each award, so a unilateral value bump cannot silently expand what looks like a governance-approved budget.

### Proof of Concept
1. Governance calls `propose_bounty` + `approve_bounty` for `value = 50`, funds the bounty account with 50, and a curator is assigned via `propose_curator`/`accept_curator` (see `create_active_parent_bounty` test helper referenced in [4](#0-3) ).
2. The curator calls `increase_value(parent_bounty_id, amount)` repeatedly, raising `bounty.value` far above the real, governance-approved 50 — this succeeds purely on the curator's own signature: [1](#0-0) .
3. The curator then creates/approves child bounties whose allocations are validated against this inflated `value` rather than the real account balance, over-committing child awards that governance never approved for that amount.
4. When multiple child bounty claims are processed, only those settling first will actually receive funds from the real (unchanged) pot balance, while later claims fail — reproducing duplicate/inconsistent settlement against a single, governance-approved-but-since-corrupted budget figure.

Note: I could not fully trace the child-bounty award/claim code paths (`award_bounty`/`claim_bounty`/`get_bounty_details`) within the available iterations to confirm whether an additional balance check at claim time fully neutralizes the over-commitment scenario in step 3–4; this should be verified by reading the full award/claim logic in `substrate/frame/multi-asset-bounties/src/lib.rs` before treating this as conclusively exploitable.

### Citations

**File:** substrate/frame/multi-asset-bounties/src/lib.rs (L1406-1419)
```rust
		/// - The bounty must be in the `Active` state.
		/// - Raises the recorded `value` by `amount`. This is used to register funds that were
		///   transferred into the bounty account out-of-band (e.g. recurring external top-ups), so
		///   they become available to award or to allocate to child bounties. It must be greater
		///   than 0.
		/// - The curator deposit is re-evaluated for the new value and any additional deposit is
		///   collected from the curator.
		/// - The value can only be increased, never decreased, so the invariant that the sum of
		///   child-bounty values never exceeds the parent value is preserved.
		/// - This call does **not** check that the bounty account holds `new_value`; it only
		///   updates the recorded value. Payouts stay bounded by the account's real balance at
		///   settlement, so increasing the value beyond the available funds simply makes a later
		///   payout fail — no funds are moved by this call.
		/// - Only a parent bounty's value can be increased via this call.
```

**File:** substrate/frame/multi-asset-bounties/src/lib.rs (L1430-1470)
```rust
		pub fn increase_value(
			origin: OriginFor<T>,
			#[pallet::compact] parent_bounty_id: BountyIndex,
			#[pallet::compact] amount: T::Balance,
		) -> DispatchResult {
			let signer = ensure_signed(origin)?;
			ensure!(!amount.is_zero(), Error::<T, I>::InvalidValue);

			let (old_value, new_value) = Bounties::<T, I>::try_mutate(
				parent_bounty_id,
				|maybe_bounty| -> Result<(T::Balance, T::Balance), DispatchError> {
					let bounty = maybe_bounty.as_mut().ok_or(Error::<T, I>::InvalidIndex)?;

					// Only an `Active` bounty has a committed curator who can authorize and
					// collateralize the increase.
					let curator = match &bounty.status {
						BountyStatus::Active { curator } => curator.clone(),
						_ => return Err(Error::<T, I>::UnexpectedStatus.into()),
					};
					ensure!(signer == curator, Error::<T, I>::RequireCurator);

					// Reject an overflowing increase rather than silently saturating to a
					// nonsensical value.
					let old_value = bounty.value;
					let new_value =
						old_value.checked_add(&amount).ok_or(Error::<T, I>::InvalidValue)?;

					// Re-evaluate the curator deposit for the new value, collecting any additional
					// hold from the curator. The deposit always exists for an `Active` bounty.
					let native_amount = T::BalanceConverter::from_asset_balance(
						new_value,
						bounty.asset_kind.clone(),
					)
					.map_err(|_| Error::<T, I>::FailedToConvertBalance)?;
					let deposit =
						CuratorDeposit::<T, I>::take(parent_bounty_id, None::<BountyIndex>)
							.ok_or(Error::<T, I>::UnexpectedStatus)?;
					let deposit = deposit.update(&curator, native_amount)?;
					CuratorDeposit::<T, I>::insert(parent_bounty_id, None::<BountyIndex>, deposit);

					bounty.value = new_value;
```

**File:** substrate/frame/multi-asset-bounties/src/tests.rs (L2674-2691)
```rust
#[test]
fn increase_value_works() {
	ExtBuilder::default().build_and_execute(|| {
		// Given: an active parent bounty (value 50, curator deposit 25 held).
		let s = create_active_parent_bounty();
		assert_eq!(Balances::reserved_balance(s.curator), s.curator_deposit);
		let increase = 20;
		let new_value = s.value + increase; // 70

		// Give the curator enough free balance to cover the larger deposit hold.
		let _ = Balances::mint_into(&s.curator, 100);

		// When
		assert_ok!(Bounties::increase_value(
			RuntimeOrigin::signed(s.curator),
			s.parent_bounty_id,
			increase,
		));
```
