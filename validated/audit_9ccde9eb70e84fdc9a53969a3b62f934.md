### Title
Attacker can permanently lock a victim's collateral-slot list via dust deposits/transfers, blocking all further deposits and transfers of new collateral types - ([File: contracts/Pool.sol / contracts/DepositToken.sol])

### Summary
`DepositToken._mint` and `DepositToken._transfer` unconditionally call `pool.addToDepositTokensOfAccount(account_)` whenever a recipient's balance for that deposit token goes from zero to non-zero (contracts/DepositToken.sol:486-488, 518-520). `Pool.addToDepositTokensOfAccount` enforces a hard cap (`MAX_TOKENS_PER_USER`) on the per-account `MappedEnumerableSet` used by `debtPositionOf`/`depositOf`. Any unprivileged attacker can fill a victim's list to the cap by calling `deposit(1 wei, victim)` (the `onBehalfOf_` parameter is unrestricted) or `transfer(victim, 1)` once per registered `DepositToken`, after which every operation that would add a new collateral type for the victim reverts — a reachable denial of service analogous to the crafted-input crash/DoS in `txt_add`.

### Finding Description
- `deposit(uint256 amount_, address onBehalfOf_)` lets any caller mint deposit tokens to an arbitrary beneficiary with no consent check; only `amount_ == 0` and `onBehalfOf_ == address(0)` are rejected (contracts/DepositToken.sol:211-237).
- `_mint` adds the token to the recipient's per-account enumerable set when their prior balance was zero (contracts/DepositToken.sol:486-488).
- `_transfer` does the same for transfer recipients (contracts/DepositToken.sol:518-520), so `transfer(victim, dust)` is an equally cheap vector.
- `Pool.addToDepositTokensOfAccount` reverts once `depositTokensOfAccount[victim]` reaches `MAX_TOKENS_PER_USER`.
- Because the revert happens inside the mint/transfer, there is no way for the victim to opt out or reject incoming tokens.

### Impact Explanation
Once the victim's set is full:
1. `deposit(x, victim)` reverts for every collateral type the victim does not already hold — the victim cannot open new collateral positions.
2. `transfer(victim, x)` / `transferFrom` / `seize` to the victim revert for any token not already in the victim's set — this also breaks `Pool.liquidate`, where the seized collateral goes to a liquidator-provided `to_` address whose set may already be full, reverting the whole liquidation (a liquidation liveness DoS that can leave unhealthy positions unliquidatable).
3. The condition is self-perpetuating until the victim fully empties one of their token balances, which requires withdrawing or transferring out 100% of at least one held collateral — costly or impossible if that collateral is locked as backing for debt (`_revertIfLocked` prevents moving locked balance, contracts/DepositToken.sol:180-182). For a victim whose balances are all locked by debt, this is an effectively indefinite freeze of new-collateral operations.

### Likelihood Explanation
- Requires only an unprivileged EOA and a few wei of each underlying asset per pool; no privileged roles, oracle manipulation, or governance needed.
- The number of distinct deposit tokens per pool is small (bounded by governance onboarding), so reaching the cap costs a handful of transactions.
- Pause/shutdown flags, `SynthContext`, and reentrancy guards do not stop it; `deposit` and `transfer` are normal user-facing entry points.

### Recommendation
- Remove the per-account cap reliance for correctness-critical paths, or make `debtPositionOf`/`depositOf` iterate a bounded subset rather than reverting on insert.
- At minimum, do not add tokens to the set on plain `transfer`/forced `deposit on behalf` without recipient opt-in; alternatively allow the set to grow but cap only the iteration by summing over a curated collateral list maintained by governance.
- For liquidation `seize`, ensure the recipient (liquidator) can always receive — e.g. seize to the pool then let liquidators claim, or bypass the per-account set for `seize`.

### Proof of Concept
Hardhat sketch (fork or fixture deploy):

```ts
// attacker = any EOA; victim = target user with open position(s)
for (const dt of allDepositTokensInPool) {
  const underlying = await ethers.getContractAt('ERC20', await dt.underlying())
  await underlying.connect(attacker).approve(dt.address, MaxUint256)
  // if victim doesn't hold dt, this inserts dt into victim's set
  await dt.connect(attacker).deposit(1, victim.address) // or dt.transfer(victim.address, 1)
}
// victim's depositTokensOfAccount length == MAX_TOKENS_PER_USER
// now any new-collateral deposit for victim reverts:
await expect(
  newDepositToken.connect(victim).deposit(parseEther('1'), victim.address)
).to.be.reverted // TokensLimitReached inside Pool.addToDepositTokensOfAccount
// and liquidations paying out a token the liquidator doesn't hold can revert on seize()
```

Confidence caveat: I verified the unconditional `addToDepositTokensOfAccount` hooks in `DepositToken.sol` and confirmed `MAX_TOKENS_PER_USER` / `addToDepositTokensOfAccount` exist in `Pool.sol` (38 matches), but I could not view `Pool.sol`'s surrounding code in this session, so the exact cap value and whether `debtPositionOf` iterates the set were inferred from the known Metronome design rather than read directly.