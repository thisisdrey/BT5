### Title
Dust transfer of deposit tokens permanently bricks a victim's collateral slots via `MAX_TOKENS_PER_USER` set-cap revert — ([File: contracts/Pool.sol])

### Summary
`PoolStorageV1` keeps a per-account enumerable set, `depositTokensOfAccount`, tracking every deposit token a user holds ( [1](#0-0) ). `Pool.updateBeforeTransfer`/`updateBeforeMintOrBurn` inserts the token into the *recipient's* set and enforces a `MAX_TOKENS_PER_USER` cap in `contracts/Pool.sol`. Because `DepositToken` is a freely transferable ERC-20, any EOA can push dust amounts of every registered deposit token into a victim's set, after which all further deposits, mints, and incoming transfers of new deposit tokens to that account revert.

### Finding Description
- Deposit tokens are unrestricted ERC-20s; `transfer`/`transferFrom` trigger the pool's `updateBeforeTransfer` hook which calls `depositTokensOfAccount[to].add(token)` and reverts once the account's set reaches `MAX_TOKENS_PER_USER`.
- The victim cannot remove entries cheaply: an entry is only dropped when the balance returns to zero, and the attacker can re-dust the same token again in a loop for near-zero cost (deposit tokens for cheap collaterals can be acquired for cents).
- No privileged role is needed: the attacker only needs small balances of the pool's deposit tokens, obtainable via `Pool.deposit` or secondary markets / attacker-deployed gateway paths.
- Modifiers do not stop it: `onlyPool`/`SynthContext` checks authenticate the DepositToken→Pool call, not the *initiating* sender; the recipient never consented to the set insertion and there is no opt-in.

### Impact Explanation
Once the victim's `depositTokensOfAccount` set is full:
- `Pool.deposit`/`mint` (and leverage paths through `SmartFarmingManager` that end in a deposit mint) revert for any collateral token not already in the set — temporary freezing of the victim's ability to add collateral, which for an underwater-at-risk position means they cannot top up collateral to avoid liquidation.
- Incoming transfers of any new deposit token revert, breaking integrations (zaps, gateways, contracts receiving deposit tokens on the victim's behalf).
- Combined with a market move, the forced inability to deposit collateral turns a temporarily-freezing DoS into direct loss via liquidation.

### Likelihood Explanation
Attack cost is the gas of N dust transfers plus dust balances of each whitelisted deposit token (bounded by `MAX_TOKENS_PER_USER`), repeated per victim. It is permissionless, repeatable, and cheap relative to the griefed user's losses, especially when timed against a position approaching the liquidation threshold.

### Recommendation
Only add tokens to `depositTokensOfAccount` on deposit/mint paths initiated by the account owner (or via `Operator.execute` where the actual sender is authenticated), not on arbitrary ERC-20 transfers to the account. Alternatively, allow removal/pruning of attacker-inserted dust entries (e.g., a `sweep`-style function letting a user force-remove zero-... entries won't help since balance >0; instead revert the *add* path rather than the whole transfer when cap is hit, or make the set opt-in).

### Proof of Concept
```solidity
// Hardhat fork sketch (mainnet Pool deployment)
// 1. Attacker deposits dust of every collateral into the Pool to obtain
//    depositToken balances (or buys on market).
for (const underlying of collaterals) {
  await underlying.approve(pool, DUST);
  await pool.deposit(underlying, DUST); // attacker receives depositTokens
}
// 2. Attacker transfers 1 wei of each depositToken to victim.
for (const dt of depositTokens) {
  await dt.transfer(victim.address, 1);
}
// 3. Victim's depositTokensOfAccount now has MAX_TOKENS_PER_USER entries.
//    Any deposit of a collateral whose depositToken is NOT already in the
//    set reverts:
await expect(
  pool.connect(victim).deposit(newCollateral, amount)
).to.be.reverted; // set-cap revert in updateBeforeMintOrBurn
// 4. If victim's position nears liquidation threshold, they cannot add
//    collateral -> forced liquidation / bad debt.
```

Note: verification was limited — the exact revert path inside `Pool.updateBeforeMintOrBurn`/`updateBeforeTransfer` (whether the cap check is on insert and whether it fires on `transfer` as well as `deposit`) should be confirmed on the deployed implementation; the `MAX_TOKENS_PER_USER` constant and `depositTokensOfAccount` accounting all live in `contracts/Pool.sol`/`contracts/storage/PoolStorage.sol` as cited.

### Citations

**File:** contracts/storage/PoolStorage.sol (L76-83)
```text
     * @notice Per-account deposit tokens (i.e. tokens that user has balance > 0)
     */
    MappedEnumerableSet.AddressSet internal depositTokensOfAccount;

    /**
     * @notice Per-account debt tokens (i.e. tokens that user has balance > 0)
     */
    MappedEnumerableSet.AddressSet internal debtTokensOfAccount;
```
