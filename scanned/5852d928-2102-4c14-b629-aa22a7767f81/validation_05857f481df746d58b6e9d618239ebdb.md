### Title
`pallet-conviction-voting` support threshold uses raw `TotalIssuance`/`ActiveIssuance` as the voting-power denominator, letting anyone permanently inflate the OpenGov quorum with un-votable tokens - (File: `substrate/frame/conviction-voting/src/types.rs`)

### Summary
This is the direct Substrate analog of the Nouns Builder bug: the `quorum`/`support` calculation is based on `token.totalSupply()` (all minted tokens) instead of the balance actually capable of casting a vote, so tokens that end up in accounts that can never vote (burned-equivalent state) still count toward the required threshold, which can be pushed up without bound and effectively wall off governance.

### Finding Description
`Tally::support()` computes the OpenGov "support" fraction as: [1](#0-0) 

`Total` here is wired in production runtimes directly to `Currency::total_issuance()` (or `active_issuance()`), with no adjustment for tokens that are provably incapable of ever voting: [2](#0-1) [3](#0-2) 

The pallet's own doc comment acknowledges this exact hazard and asks integrators to hand-tune it: *"May just be `Currency::total_issuance`, but you might want to reduce this in order to account for funds in the system which are unable to vote (e.g. parachain auction deposits)."* [4](#0-3) 

Total issuance is unconditionally incremented by `fungible::Mutate::mint_into`, which any unprivileged user can trigger through the XCM `TransactAsset` teleport/mint path (`FungibleMutateAdapter::accrue_checked` → `Fungible::mint_into`) by specifying an arbitrary `beneficiary` `Location` for the deposit: [5](#0-4) [6](#0-5) 

If the caller specifies a beneficiary account that has no corresponding private key (a hash-derived "unspendable" `Location`/`AccountId32`, e.g. an all-zero or non-preimageable junction), the minted tokens permanently increase `TotalIssuance`/`ActiveIssuance` while never being able to sign an extrinsic to vote. Every subsequent referendum's `support` denominator (`Total::get()`) grows by that amount with zero corresponding voting power, exactly mirroring the report's `assumedVotingPower` vs `realVotingPower` gap. Unlike slashing (which burns and *reduces* issuance, self-correcting the denominator), there is no mechanism that detects or excludes tokens stuck in unreachable accounts — the guard the report calls for ("decrease supply when burned" or "adjust quorum for non-voting holdings") does not exist for this path.

### Impact Explanation
Because `support` and `approval` gate every referendum from entering the "Passing"/"Confirming" state in `Referenda::is_passing`, a permanently inflated `Total::get()` denominator raises the bar for support on every track (root, treasury, staking-admin, etc.) simultaneously and irreversibly. Enough accumulated un-votable issuance can make legitimate tracks unable to reach their `min_support` curve even under full honest turnout, functionally freezing on-chain governance (root track upgrades, treasury spends, emergency cancels) — a "runtime bug that compromises intended behavior" with a persistent, unprivileged, low-cost griefing vector, and no privileged/relayer/validator assumption required. [7](#0-6) 

### Likelihood Explanation
Likelihood is moderate: the mechanism requires only an ordinary account executing an XCM teleport/mint sequence with a chosen unreachable beneficiary `Location`, repeated at the attacker's own cost (their own tokens are what get "burned" into an unreachable account, similar to the auction bidder abstaining in the original report). No governance, admin, relayer, or validator collusion is needed. The economic cost scales with the amount of issuance the attacker wants to add un-votable, which bounds but does not eliminate feasibility for a well-funded actor targeting a specific track.

### Recommendation
- Do not let `MaxTurnout`/`Total` default to raw `total_issuance()`/`active_issuance()` in production runtimes; track and subtract balances that are provably unspendable/un-votable (e.g. via a registry of known burn/unspendable locations, or by excluding externally-minted/teleported deposits to unverified beneficiaries from `active_issuance`).
- Consider having XCM `deposit_asset`/mint paths call `deactivate()` for deposits to accounts that fail a "controllable" check, so such issuance never inflates the conviction-voting denominator.
- Alternatively, compute quorum/support against a snapshot of provably-active (recently transacted or delegate-registered) issuance rather than total issuance.

### Proof of Concept
Conceptual/parametrized (mirrors the original PoC structure since this analog is a systemic design property rather than a single reproducible unit test in this environment):
1. Attacker holds `X` DOT/KSM on a system parachain.
2. Attacker issues a teleport (`pallet_xcm::teleport_assets` / `limited_teleport_assets`) to the relay chain (or another system chain configured with `FungibleAdapter`/`FungibleMutateAdapter` as `TransactAsset`), setting the `beneficiary` to an `AccountId32` derived Location with no known private key.
3. On execution, `FungibleMutateAdapter::deposit_asset` → `accrue_checked` → `Fungible::mint_into` increases destination-chain `TotalIssuance`/`ActiveIssuance` by `X`, crediting an address nobody can sign for (substrate/frame/support/src/traits/tokens/fungible/regular.rs:249-255; polkadot/xcm/xcm-builder/src/fungible_adapter.rs:98-126).
4. Repeat to accumulate `ΔIssuance` = sum of all such unreachable deposits.
5. Submit a referendum on any OpenGov track; compute `support = Perbill::from_rational(status.tally.support, Total::get())` where `Total::get()` now includes `ΔIssuance` (substrate/frame/conviction-voting/src/types.rs:68-70).
6. As `ΔIssuance` grows relative to real circulating/votable supply, `support` for a fixed amount of honest votes trends toward zero, and `Referenda::is_passing` (substrate/frame/referenda/src/lib.rs:1318-1329) can never be satisfied for that track, blocking approval regardless of turnout — the same "quorum unreachable even with full participation" outcome demonstrated in the original Nouns Builder PoC.

**Caveat:** I was not able to execute this against a live runtime/test harness in this environment, so the concrete numeric feasibility (cost vs. issuance impact per runtime) is unverified; the code paths cited do confirm the mechanism (unconditional `total_issuance` increase on mint, and `Total::get()` used unmodified as the support denominator) exists as designed, with the pallet's own documentation flagging exactly this risk.

### Citations

**File:** substrate/frame/conviction-voting/src/types.rs (L68-70)
```rust
	fn support(&self, _: Class) -> Perbill {
		Perbill::from_rational(self.support, Total::get())
	}
```

**File:** substrate/bin/node/runtime/src/lib.rs (L1080-1090)
```rust
impl pallet_conviction_voting::Config for Runtime {
	type WeightInfo = pallet_conviction_voting::weights::SubstrateWeight<Self>;
	type RuntimeEvent = RuntimeEvent;
	type Currency = Balances;
	type VoteLockingPeriod = VoteLockingPeriod;
	type MaxVotes = ConstU32<512>;
	type MaxTurnout = frame_support::traits::TotalIssuanceOf<Balances, Self::AccountId>;
	type Polls = Referenda;
	type BlockNumberProvider = System;
	type VotingHooks = ();
}
```

**File:** substrate/frame/support/src/traits/tokens/currency.rs (L218-234)
```rust
/// A non-const `Get` implementation parameterised by a `Currency` impl which provides the result
/// of `total_issuance`.
pub struct TotalIssuanceOf<C: Currency<A>, A>(core::marker::PhantomData<(C, A)>);
impl<C: Currency<A>, A> Get<C::Balance> for TotalIssuanceOf<C, A> {
	fn get() -> C::Balance {
		C::total_issuance()
	}
}

/// A non-const `Get` implementation parameterised by a `Currency` impl which provides the result
/// of `active_issuance`.
pub struct ActiveIssuanceOf<C: Currency<A>, A>(core::marker::PhantomData<(C, A)>);
impl<C: Currency<A>, A> Get<C::Balance> for ActiveIssuanceOf<C, A> {
	fn get() -> C::Balance {
		C::active_issuance()
	}
}
```

**File:** substrate/frame/conviction-voting/src/lib.rs (L128-131)
```rust
		/// The maximum amount of tokens which may be used for voting. May just be
		/// `Currency::total_issuance`, but you might want to reduce this in order to account for
		/// funds in the system which are unable to vote (e.g. parachain auction deposits).
		type MaxTurnout: Get<BalanceOf<Self, I>>;
```

**File:** polkadot/xcm/xcm-builder/src/fungible_adapter.rs (L98-126)
```rust
	fn can_accrue_checked(checking_account: AccountId, amount: Fungible::Balance) -> XcmResult {
		Fungible::can_deposit(&checking_account, amount, Minted)
			.into_result()
			.map_err(|error| {
				tracing::debug!(
					target: "xcm::fungible_adapter", ?error, ?checking_account, ?amount,
					"Failed to deposit funds into account",
				);
				XcmError::NotDepositable
			})
	}

	fn can_reduce_checked(checking_account: AccountId, amount: Fungible::Balance) -> XcmResult {
		Fungible::can_withdraw(&checking_account, amount)
			.into_result(false)
			.map_err(|error| {
				tracing::debug!(
					target: "xcm::fungible_adapter", ?error, ?checking_account, ?amount,
					"Failed to withdraw funds from account",
				);
				XcmError::NotWithdrawable
			})
			.map(|_| ())
	}

	fn accrue_checked(checking_account: AccountId, amount: Fungible::Balance) {
		let ok = Fungible::mint_into(&checking_account, amount).is_ok();
		debug_assert!(ok, "`can_accrue_checked` must have returned `true` immediately prior; qed");
	}
```

**File:** substrate/frame/support/src/traits/tokens/fungible/regular.rs (L247-255)
```rust
	/// Increase the balance of `who` by exactly `amount`, minting new tokens. If that isn't
	/// possible then an `Err` is returned and nothing is changed.
	fn mint_into(who: &AccountId, amount: Self::Balance) -> Result<Self::Balance, DispatchError> {
		Self::total_issuance().checked_add(&amount).ok_or(ArithmeticError::Overflow)?;
		let actual = Self::increase_balance(who, amount, Exact)?;
		Self::set_total_issuance(Self::total_issuance().saturating_add(actual));
		Self::done_mint_into(who, amount);
		Ok(actual)
	}
```

**File:** substrate/frame/referenda/src/lib.rs (L1315-1329)
```rust
	/// Determine whether the given `tally` would result in a referendum passing at `elapsed` blocks
	/// into a total decision `period`, given the two curves for `support_needed` and
	/// `approval_needed`.
	fn is_passing(
		tally: &T::Tally,
		elapsed: BlockNumberFor<T, I>,
		period: BlockNumberFor<T, I>,
		support_needed: &Curve,
		approval_needed: &Curve,
		id: TrackIdOf<T, I>,
	) -> bool {
		let x = Perbill::from_rational(elapsed.min(period), period);
		support_needed.passing(x, tally.support(id)) &&
			approval_needed.passing(x, tally.approval(id))
	}
```
