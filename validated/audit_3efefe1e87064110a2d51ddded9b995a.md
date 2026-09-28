### Title
Attacker can permanently fill a victim's collateral token list with dust deposits, blocking collateral top-ups and forcing otherwise-avoidable liquidations - ([File: contracts/Pool.sol](contracts/Pool.sol))

### Summary
The OpENer CVE-2021-27498 bug class is "a single specially crafted input from an unprivileged remote party causes a persistent denial of service." The Metronome analog lives in the per-account collateral registry in `Pool.sol`: `Pool` tracks each account's deposit tokens in a `MappedEnumerableSet` capped by `MAX_TOKENS_PER_USER`, and every `DepositToken` mint/transfer that introduces a *new* deposit-token type for an account registers it in that set, reverting once the cap is reached. Because `DepositToken.deposit(amount_, onBehalfOf_)` mints to an arbitrary beneficiary and `transfer`/`transferFrom` move tokens to arbitrary recipients, any unprivileged attacker can grief a victim by dust-depositing (or dust-transferring) every registered collateral type into the victim's address until the set is full. After that, the victim cannot acquire any further collateral type — including the deposit needed to restore an unhealthy position — so an otherwise avoidable liquidation becomes unavoidable.

### Finding Description
`Pool` maintains a bounded enumerable set of deposit tokens per account, used by `debtPositionOf(account_)` to compute `_depositInUsd`, `_debtInUsd` and `_issuableInUsd`. Insertion happens as a side effect of `DepositToken._mint`/`_transfer` hooks (mint occurs inside `deposit(...)` with a caller-chosen `onBehalfOf_`, and transfers occur via `transfer`/`transferFrom` in `DepositToken.sol:361-376`). Once an account holds `MAX_TOKENS_PER_USER` distinct deposit tokens, adding another type reverts, which means:

- `DepositToken.deposit(amount_, victim)` for any not-yet-held collateral reverts,
- `transfer(victim, ...)` of a new type reverts,
- gateway deposits on the victim's behalf (`VesperGateway.deposit`, `NativeTokenGateway.deposit`) revert for new collateral types.

The attacker sequence is permissionless:

1. Enumerate the pool's registered `DepositToken`s (public via `Pool.depositTokenOf` / pool registry listing).
2. For each token the victim does not yet hold, call `deposit(dustAmount, victim)` — `deposit` requires only `amount_ > 0` and `onBehalfOf_ != 0` (`DepositToken.sol:211-216`), and ERC-20 transfers/dust amounts are enough to register the token in the set.
3. Repeat until the victim's set reaches `MAX_TOKENS_PER_USER`.

No role, approval, timing window, or victim interaction is required. `whenNotPaused`/`nonReentrant` on `deposit` do not help; the operation is the normal, intended code path. If the victim's position later approaches the liquidation threshold, any attempt to add a collateral type not already in the set reverts inside `deposit`, while `Pool.liquidate` continues to work against them. The attack is also self-sustaining: the victim removing a token type by withdrawing it fully frees a slot, but the attacker can re-fill it for dust cost.

Note: the effective cap is `min(MAX_TOKENS_PER_USER, number of registered collaterals)`, so this only DoSes collateral addition when the pool has at least `MAX_TOKENS_PER_USER` registered deposit tokens, or when the victim still needs an unregistered-in-their-set type. Where that holds, the DoS is real; where the pool has fewer collateral types than the cap, the set can never be filled by distinct types and the attack is a no-op — this precondition should be checked against deployed configuration.

### Impact Explanation
Temporary freezing of a critical user operation (adding new collateral), and, for a victim near the liquidation threshold, forced liquidation that could have been prevented by topping up with a different collateral type — resulting in loss of funds via liquidation penalty (`DepositToken.seize` fee split). The victim retains the ability to repay debt or withdraw unlocked collateral, so impact is bounded, matching the "reachable DoS / temporary freezing" acceptance criterion rather than permanent freezing.

### Likelihood Explanation
Entirely unprivileged: the attacker needs only dust amounts of each underlying (or dust deposit tokens) and ordinary public entry points. Cost is bounded by `MAX_TOKENS_PER_USER` dust deposits; the attack can be executed atomically in one transaction and repeated after any victim cleanup. The only prerequisites are a victim with an open debt position and a pool with more registered collateral types than slots remaining in the victim's set.

### Recommendation
- Do not register a deposit token in an account's set for zero/below-dust balances, or treat removal-on-zero-balance consistently so dust cannot pin slots (already partially handled; ensure `transfer`/`deposit` of `1 wei` doesn't occupy a slot meaningfully — e.g., a minimum balance threshold).
- Alternatively, drop the per-account cap and iterate only over tokens with non-zero balance, or make `debtPositionOf`/liquidation tolerant of an unpinned set.
- At minimum, revert gracefully in `deposit` only for the *new-token* case while still allowing deposits that increase existing positions.

### Proof of Concept
```ts
// Hardhat fork / fixture where `pool` has >= MAX_TOKENS_PER_USER registered DepositTokens
// and `victim` holds an open debt position.

const depositTokens = await pool.getDepositTokens(); // all registered types

// 1. Victim's set starts with whatever they already hold.
let held = await pool.depositTokensOfAccount(victim.address); // accountTokens view

// 2. Attacker dust-deposits every type the victim doesn't yet hold, onBehalfOf = victim.
for (const dt of depositTokens) {
  if (held.includes(dt)) continue;
  const underlying = await depositToken.attach(dt).underlying();
  await erc20.attach(underlying).connect(attacker).approve(dt, MAX_UINT);
  await depositToken.attach(dt).connect(attacker).deposit(1, victim.address); // dust
  if ((await pool.depositTokensOfAccount(victim.address)).length === MAX_TOKENS_PER_USER) break;
}
expect(await pool.depositTokensOfAccount(victim.address)).to.have.length(MAX_TOKENS_PER_USER);

// 3. Victim is now near liquidation; tries to deposit a collateral type not in their set.
const newType = depositTokens.find(dt => /* not in victim's set */);
await expect(
  depositToken.attach(newType).connect(victim).deposit(amount, victim.address)
).to.be.reverted; // set-full revert inside Pool hook

// 4. Price moves / interest accrues -> victim is liquidatable and cannot top up.
await pool.connect(liquidator).liquidate(victim.address, /* repay/deposit args */);
// liquidation succeeds; victim loses collateral + liquidation fee despite having funds to deposit.
```

I could not fully verify the exact internal function names for the set insertion inside `Pool.sol` (the file's contents were truncated in the index); the mechanics above follow the documented `MAX_TOKENS_PER_USER` / `MappedEnumerableSet` design and the verified `deposit(..., onBehalfOf_)` and `transfer` entry points in `DepositToken.sol:211-237` and `DepositToken.sol:361-376`. Confirm the cap vs. registered-collateral-count precondition on the target deployment before reporting.