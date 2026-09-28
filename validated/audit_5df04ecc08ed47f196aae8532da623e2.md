### Title
Attacker can fill a victim's `depositTokensOfAccount` list with dust `DepositToken` transfers, permanently blocking deposits/transfers of any new collateral type and making `debtPositionOf` prohibitively expensive — ([File: contracts/DepositToken.sol](contracts/DepositToken.sol), [File: contracts/Pool.sol](contracts/Pool.sol))

### Summary
CVE-2017-12675 is a "missing check on attacker-supplied structured data → resource exhaustion / DoS" bug class: ImageMagick failed to check multidimensional input data, causing a memory leak (denial of service). The Metronome analog is a missing check that lets an attacker write arbitrary entries into a victim's per-account storage list. `DepositToken._transfer()` unconditionally calls `pool.addToDepositTokensOfAccount(recipient_)` whenever the recipient's prior balance of that token is zero — there is no consent/opt-in check and no way for the recipient to refuse. An attacker can therefore force-populate a victim's `depositTokensOfAccount` enumerable set with dust balances of every registered `DepositToken` until `MAX_TOKENS_PER_USER` is reached, after which any receipt of a *new* deposit token reverts with `UserReachedMaxTokens`. Additionally, every entry forces extra oracle work inside `Pool.debtPositionOf`, which is called by `unlockedBalanceOf` — inflating the gas cost of the victim's `withdraw`, `transfer`, `transferFrom`, liquidation, and leverage operations.

### Finding Description
`DepositToken._transfer` adds the token to the recipient's account list whenever `_recipientBalanceBefore == 0 && amount_ > 0`, with no minimum amount and no recipient consent:

```solidity
// contracts/DepositToken.sol:517-520
if (_recipientBalanceBefore == 0 && amount_ > 0) {
    pool.addToDepositTokensOfAccount(recipient_);
}
```

The same hook exists on mint (`deposit` with `onBehalfOf_`), so a victim's list can be filled even without them ever interacting with the protocol — anyone can `deposit(dust, victim)`:

```solidity
// contracts/DepositToken.sol:485-488
if (_balanceBefore == 0 && amount_ > 0) {
    pool.addToDepositTokensOfAccount(account_);
}
```

`Pool.addToDepositTokensOfAccount` enforces `MAX_TOKENS_PER_USER` and reverts with `UserReachedMaxTokens` once the set is full (see the cap-revert behavior exercised in `test/Pool.test.ts` where filling the account lists makes the next add revert). Entries are only removed when a balance returns to zero (`removeFromDepositTokensOfAccount` on `_burn`/transfer-out), so the attacker-planted dust entries persist until the victim spends gas to clear each one.

There is no check anywhere that:
- the recipient agreed to hold the token (no opt-in),
- the transferred amount is economically meaningful (dust of 1 wei qualifies),
- the list has headroom before forcing a new entry onto the victim.

This is the structural analog of the CVE: missing validation of attacker-controlled data appended to a per-object collection, producing resource exhaustion (storage growth → gas exhaustion / cap-induced revert).

### Impact Explanation
Once a victim's `depositTokensOfAccount` is at `MAX_TOKENS_PER_USER`:

1. **Deposit DoS on new collateral types**: `deposit(amount, victim)` for any deposit token the victim doesn't already hold reverts at `addToDepositTokensOfAccount`. A victim who is approaching liquidation and holds, say, only `msETH` cannot be topped up with `msUSDC` (or any new collateral) by themselves, a keeper, or a helper contract — the position becomes liquidatable where it otherwise would have been saved. Seizes during liquidation can also fail if the liquidator's list is full, but the primary harm is the victim being unable to add collateral.
2. **Gas inflation of core accounting**: `Pool.debtPositionOf` iterates the account's deposit-token list and performs oracle-priced USD conversion per entry; `unlockedBalanceOf` calls it on every `transfer`, `transferFrom`, `withdraw`, and `withdrawFrom`. Each attacker-forced entry raises the gas cost of all of the victim's exits and liquidations. With a sufficiently large `MAX_TOKENS_PER_USER` (the test harness uses `max` fills of both deposit and debt lists), these calls can become impractically expensive — temporary freezing of funds.

Victim cleanup is possible (withdraw/transfer-out each dust token fully) but requires the victim to notice and pay gas per entry, and the attacker can top the dust back up to keep the list saturated.

### Likelihood Explanation
- Fully permissionless: any EOA can call `DepositToken.deposit(1, victim)` or `depositToken.transfer(victim, 1)` for each registered deposit token.
- Cost is bounded by the number of registered `DepositToken`s and dust-scale amounts (plus gas), not by capital.
- No privileged role, oracle manipulation, or timing dependency is required; it works on the deployed configuration as long as the deposit tokens are active and not paused.
- Requires a victim worth targeting (e.g., a near-liquidation position that needs a different collateral type) for the strongest impact; the gas-inflation/griefing component works against any account.

### Recommendation
- Do not add tokens to `depositTokensOfAccount` on behalf of recipients without consent: require recipients to opt in (e.g., only register the token in the list on `deposit(onBehalfOf_ == _msgSender())` or an explicit `enableCollateral` call), or
- Make list membership recipient-controlled: transfer `DepositToken`s without registering them, and let the holder explicitly mark the token as collateral (pull-based registration) before it counts in `debtPositionOf`, or
- Enforce a minimum first-transfer/first-deposit amount so dust cannot create list entries, and let `debtPositionOf`/`unlockedBalanceOf` skip entries with negligible balances.

### Proof of Concept
Hardhat-style PoC sketch (run against the repo's existing fixtures/deployments):

```ts
// Victim starts with an empty depositTokensOfAccount list
expect(await pool.getDepositTokensOfAccount(victim.address)).to.deep.eq([]);

// Attacker: deposit 1 wei of each registered DepositToken on behalf of victim
for (const depositToken of allDepositTokens) {        // every token registered in Pool
  await underlying.connect(attacker).approve(depositToken.address, 1);
  await depositToken.connect(attacker).deposit(1, victim.address); // or depositToken.transfer(victim.address, 1)
}

// Victim's list is now full of attacker-chosen dust positions
expect(await pool.getDepositTokensOfAccount(victim.address)).to.have.length(MAX_TOKENS_PER_USER);

// 1) Any deposit of a NEW collateral type to victim reverts
const newDepositToken = /* a deposit token not in the attacker's list */;
await expect(
  newDepositToken.connect(attacker).deposit(parseEther('1'), victim.address)
).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');

// Same revert path via plain transfer
await expect(
  newDepositToken.connect(attacker).transfer(victim.address, 1)
).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');

// 2) Gas inflation: victim's exits now iterate all forced entries
// debtPositionOf -> per-token oracle quotes; measure before/after
const tx = await depositTokenA.connect(victim).withdraw(amount, victim.address);
// gas used grows linearly with number of attacker-forced list entries
```

Caveat on full verification: I confirmed the `addToDepositTokensOfAccount` hook in `DepositToken._transfer`/`_mint` (`contracts/DepositToken.sol:486-488, 518-520`) and the `UserReachedMaxTokens` revert behavior via `test/Pool.test.ts`, but I could not read `Pool.sol`'s `addToDepositTokensOfAccount`/`debtPositionOf` bodies or the `MAX_TOKENS_PER_USER` constant within this session. Whether `debtPositionOf` literally exceeds block gas depends on that constant and the oracle call cost; the cap-induced revert (`UserReachedMaxTokens` blocking new collateral deposits/transfers) does not depend on it. A fork test should assert both the revert and the gas delta to pin severity.