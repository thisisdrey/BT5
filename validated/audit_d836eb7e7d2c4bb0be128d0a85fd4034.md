### Title
Attacker can permanently fill a victim's `depositTokensOfAccount` set to `MAX_TOKENS_PER_USER` via dust transfers, bricking all new debt issuance, new collateral deposits, and token receipt - ([File: contracts/Pool.sol](contracts/Pool.sol))

### Summary
`Pool` enforces a hard cap of `MAX_TOKENS_PER_USER = 30` on the combined size of `debtTokensOfAccount` + `depositTokensOfAccount` for every account via `onlyIfAdditionWillNotReachMaxTokens`. Entries are added to `depositTokensOfAccount` whenever an account's `DepositToken` balance goes from `0` to non-zero — including through plain, permissionless `DepositToken.transfer`/`transferFrom`. An unprivileged attacker can mint dust balances of every registered `DepositToken` and `transfer` 1 wei of each to a victim, permanently occupying all 30 slots. From then on, any operation that would add a *new* token to the victim's set reverts with `UserReachedMaxTokens`.

### Finding Description
- `DepositToken._transfer` calls `pool.addToDepositTokensOfAccount(recipient_)` whenever the recipient's prior balance was `0` (contracts/DepositToken.sol:517-520).
- `Pool.addToDepositTokensOfAccount` is gated by `onlyIfAdditionWillNotReachMaxTokens`, which reverts once `debtTokensOfAccount.length + depositTokensOfAccount.length >= 30` (contracts/Pool.sol:143-148, 216-220).
- There is no minimum amount, no opt-in, and no way to reject inbound `DepositToken` transfers — any EOA can push entries into any other account's set at dust cost.
- The analogous failure to CVE-2026-20066: crafted inputs (dust transfers) hit a normalization/bookkeeping path that aborts processing — here every subsequent `add` reverts, denying service for that account.
- Once the set is saturated:
  - `DebtToken.issue`/`mint`/`flashIssue` for a synth the victim doesn't already hold debt in → `addToDebtTokensOfAccount` reverts → victim cannot borrow/open new debt positions.
  - `DepositToken.deposit(..., onBehalfOf_ = victim)` or transfers of a deposit token the victim doesn't hold → revert → victim cannot receive collateral of new types, and `Pool.swap` proceeds are unaffected but `SmartFarmingManager.leverage` flows that mint to a fresh sub-account or contract can be bricked.
  - For contract-based accounts that cannot themselves call `transfer` to clear slots (e.g., positions held by `SmartFarmingManager` or other integrator contracts), the set can never be emptied — the DoS is permanent.
- The cap also counts debt tokens, so a victim holding even a few debt positions needs fewer dust tokens to saturate.
- Modifiers do not mitigate: `transfer` on `DepositToken` has no pause check gate for the `add`, no amount floor, and `SynthContext`/reentrancy guards are irrelevant — the attack is plain ERC20 transfers.

### Impact Explanation
Targeted denial of service / freezing of position-management functionality: the victim is permanently unable to (a) issue debt in any new synthetic asset, (b) deposit collateral types not already in their set, and (c) receive any `DepositToken` they don't already hold — the last also breaks third-party rescue attempts (someone cannot send the victim the collateral needed to cure an unhealthy position in a new token type; only pre-held dust types work). For contract-held accounts lacking a generic escape hatch, this is a permanent freeze of the account's ability to operate — a liveness/conservation break on the per-account token registry invariant, directly matching the bug class (unauthenticated remote input → interruption of processing).

### Likelihood Explanation
High feasibility: cost is ~30 × (deposit dust + transfer gas). Attacker can deposit minimum amounts of each collateral via `DepositToken.deposit`, then transfer 1 wei of each to the victim. Fully permissionless, no privileged role, no oracle manipulation, works on the deployed configuration since `MAX_TOKENS_PER_USER` is a hardcoded constant and the `add` path has no opt-out.

### Recommendation
- Enforce a minimum first-deposit/transfer amount (e.g., a USD floor quoted via `masterOracle`) before calling `addToDepositTokensOfAccount`.
- Alternatively, allow removal-forced eviction (let `add` succeed by evicting the lowest-value dust entry) or let recipients sweep unwanted tokens via a `pull`-based opt-in model.
- At minimum, document that contract integrators must expose a way to call `DepositToken.transfer` to reclaim set slots.

### Proof of Concept
Hardhat sketch against deployed contracts:

```ts
// Attacker: for each of the 30 registered deposit tokens
for (const dt of depositTokens.slice(0, 30)) {
  // attacker already holds dust (deposited min amount earlier)
  await dt.connect(attacker).transfer(victim.address, 1);
}
// victim's set now has 30 entries
expect(await pool.getDepositTokensOfAccount(victim.address)).to.have.lengthOf(30);

// 1) victim cannot mint a new synth (adds a debt token)
await expect(
  debtToken.connect(victim).issue(syntheticToken, victim.address) // or Pool->DebtToken.mint path
).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');

// 2) victim cannot receive a new deposit token type / deposit onBehalf
const newDt = depositTokens[30]; // newly added collateral
await expect(
  newDt.connect(attacker).transfer(victim.address, 1)
).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');

// 3) attacker re-fills any slot the victim frees, sustaining the DoS cheaply
```

Limitation: a plain EOA victim can escape by atomically freeing a slot (`transfer` dust out then acting) in one transaction, but the attacker can re-saturate, and contract-held accounts without a generic transfer capability cannot escape at all.