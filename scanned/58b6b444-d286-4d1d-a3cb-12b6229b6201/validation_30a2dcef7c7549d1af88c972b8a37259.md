Based on the investigation, the most consistent local analog to the "post-commitment parameter mutation leading to fund theft" bug class is in `substrate/frame/multi-asset-bounties`, where a bounty's `value` can be increased by the curator **after** the bounty is already `Active` (i.e., after curator assignment/work has effectively started), and that increased value is what gets paid out at `award_bounty` / claim time — mirroring the reported pattern of mutating committed economic parameters (price/currency in the external report) after the point where they should have been frozen.

I was not able to fully inspect the internal guard logic of `increase_value` and its funding-source checks in `substrate/frame/multi-asset-bounties/src/lib.rs` before running out of tool budget (the file was large and my last `grep_search` returned only match counts, not the code body). This is a genuine limitation of this analysis — I confirmed the *externally observable behavior* via the pallet's own test suite, but not the exact reservation/escrow bookkeeping that would show whether the additional value is actually backed by additional funds transferred into the bounty's dedicated account before payout.

### Title
Bounty value can be increased and paid out after the bounty is already Active, without a corresponding funding check - (File: substrate/frame/multi-asset-bounties/src/lib.rs)

### Summary
`pallet-multi-asset-bounties` exposes an `increase_value` extrinsic that mutates a bounty's `value` while the bounty is already `Active` (curator assigned, work potentially already delivered). The subsequent `award_bounty` / payout path pays the beneficiary the *new*, increased value rather than the value that existed when the bounty was funded/approved, as demonstrated by the pallet's own test.

### Finding Description
The pallet's test `increase_value_then_award_pays_new_value` shows that after a parent bounty is active and funded at `value = 50`, the curator can call `increase_value` (increase = 20) to bring `value` to 70, then call `award_bounty`, and the beneficiary is paid the new value of 70 instead of the originally funded 50: [1](#0-0) 

This is structurally the same broken invariant as the external report: an economic parameter (raise price/currency in the report; bounty `value` here) that should be immutable once the bounty has moved past its "configurable" phase (i.e., once curator/work commitment has begun) can still be changed, and the changed value is honored at final settlement without the guard being tied to whether the funding backing that parameter was updated in lockstep. In the external report this let a project owner swap `currency` after tokens were already minted in presale; here it lets a value bump be honored at `award_bounty`/claim time without a proven equivalent re-verification that the bounty's on-chain asset balance actually covers the new value for the specific `asset_kind`.

### Impact Explanation
If `increase_value` does not require (and atomically settle) an equivalent top-up of the bounty's dedicated asset balance before the increase is accepted, then awarding/claiming the bounty at the new value can either fail (denial of service on the payout) or — more critically — pay out more than what was deposited for that specific bounty, which for a treasury-funded, asset-kind-parameterized bounty system directly maps to "theft or unbacked payout" and "duplicate/incorrect settlement" impact categories called out in the impact gate.

### Likelihood Explanation
The likelihood assessment is incomplete: I could not verify from the code whether `increase_value` performs a compensating fund transfer/reservation check before allowing the bump, since I ran out of budget to read the full extrinsic body (only match locations, not code, were retrieved for `substrate/frame/multi-asset-bounties/src/lib.rs`). If such a check exists (e.g., requiring the curator to top up the bounty account before `increase_value` succeeds), this would not be exploitable and the finding would be a false positive. This must be verified directly in a follow-up session.

### Recommendation
Before shipping this as a confirmed finding, a Devin session should:
1. Read the full `increase_value` and `award_bounty`/`claim_bounty` implementations in `substrate/frame/multi-asset-bounties/src/lib.rs` to determine whether the increased `value` is backed by an equivalent, atomic increase in the bounty's reserved/escrowed balance for the specific `asset_kind`.
2. If no such backing check exists, add a check that `increase_value` only succeeds after the caller has deposited the incremental amount into the bounty's dedicated account (or blocks the increase after the bounty has left an initial "Funding" phase, mirroring the fix pattern from the external report — freeze economically consequential fields once execution/commitment has started).
3. Add a regression test asserting that `award_bounty`/`claim_bounty` cannot pay out more than what is actually held in the bounty's own asset balance.

### Proof of Concept
Reference reproduction already exists in the pallet's own test suite: [1](#0-0) 
1. Create and fund an active parent bounty with `value = 50` (`s = create_active_parent_bounty()`).
2. Curator calls `Bounties::increase_value(curator, parent_bounty_id, 20)`, bumping `value` to 70 — with no evidence in the visible test flow that the curator transferred the extra 20 into the bounty's own funded balance prior to this call (only `Balances::mint_into(&s.curator, 100)` funds the curator's personal account, not the bounty account).
3. Curator calls `award_bounty(...)`.
4. `approve_payment` settles at `new_value` (70), and the emitted `BountyPayoutProcessed` event confirms the beneficiary is paid 70, not the originally committed 50.

**Caveat:** because I did not confirm the exact fund-reservation mechanics inside `increase_value`, this should be treated as a strong candidate requiring direct code confirmation, not a fully proven exploit.

### Citations

**File:** substrate/frame/multi-asset-bounties/src/tests.rs (L2756-2789)
```rust
#[test]
fn increase_value_then_award_pays_new_value() {
	ExtBuilder::default().build_and_execute(|| {
		// Given: an active parent bounty, value increased from 50 to 70.
		let s = create_active_parent_bounty();
		let _ = Balances::mint_into(&s.curator, 100);
		let increase = 20;
		let new_value = s.value + increase;
		assert_ok!(Bounties::increase_value(
			RuntimeOrigin::signed(s.curator),
			s.parent_bounty_id,
			increase,
		));

		// When: the bounty is awarded and the payout settles.
		assert_ok!(Bounties::award_bounty(
			RuntimeOrigin::signed(s.curator),
			s.parent_bounty_id,
			None,
			s.beneficiary,
		));
		approve_payment(s.beneficiary, s.parent_bounty_id, None, s.asset_kind, new_value);

		// Then: the beneficiary is paid the NEW value, not the original.
		expect_events(vec![BountiesEvent::BountyPayoutProcessed {
			index: s.parent_bounty_id,
			child_index: None,
			asset_kind: s.asset_kind,
			value: new_value,
			beneficiary: s.beneficiary,
		}]);
		assert_eq!(paid(s.beneficiary, s.asset_kind), new_value);
	});
}
```
