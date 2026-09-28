### Title
Attacker can fill a victim's per-account token list with dust deposit-token transfers, permanently blocking new collateral deposits and emergency deleveraging (griefing DoS) - ([File: contracts/Pool.sol])

### Summary
`Pool` tracks every `DepositToken`/`DebtToken` an account has ever touched in `depositTokensOfAccount` / `debtTokensOfAccount` and enforces `MAX_TOKENS_PER_USER = 30`. Because `DepositToken._transfer` (and `mint`/`seize`) unconditionally calls `Pool.addToDepositTokensOfAccount` for any recipient whose balance moves from 0 to non-zero, any unprivileged user can push dust amounts of every registered deposit token to a victim address until the victim's combined list length hits 30. From that point, any action that would add a *new* token to the victim's lists reverts with `UserReachedMaxTokens` — including the victim depositing a different collateral to cure an underwater position, or receiving `msdToken` via `seize` in a liquidation. This mirrors the CVE-2019-2507 bug class: a remotely triggerable, low-cost denial of service that hangs/crashes legitimate operations (here: reverts every state-changing path that needs a new set entry).

### Finding Description
- `Pool.onlyIfAdditionWillNotReachMaxTokens` reverts when `debtTokensOfAccount.length(account_) + depositTokensOfAccount.length(account_) >= 30` [1](#0-0) .
- `addToDepositTokensOfAccount` is gated only by "caller is a registered deposit token" — it does not check that the recipient consented [2](#0-1) .
- `DepositToken._transfer` calls `pool.addToDepositTokensOfAccount(recipient_)` whenever the recipient balance transitions from 0 [3](#0-2) ; the same hook runs on `_mint` [4](#0-3) .
- `depositToken_.seize` in `Pool.liquidate` routes through the same add path for the liquidator/fee collector [5](#0-4) .

Attack trace (all public, no privilege):
1. Attacker deposits a tiny amount of each underlying into every registered `DepositToken` (or buys dust on the open market) so they hold transferable `msd*` balances.
2. Attacker calls `msdToken_i.transfer(victim, 1)` for each distinct deposit token `i` until `depositTokensOfAccount.length(victim) + debtTokensOfAccount.length(victim) == 30`.
3. Any subsequent call that would add a new token to the victim's set reverts: `DepositToken.deposit`/`mint` of a collateral type the victim doesn't yet hold, `transfer`/`transferFrom` of a new `msd*` to the victim, and `seize` to a maxed-out liquidator/fee collector.
4. Concretely: a victim whose position turns unhealthy cannot deposit a *different* collateral to restore health — the `mint` inside `DepositToken.deposit` reverts inside `addToDepositTokensOfAccount` — so liquidation proceeds against a position that could have been cured.

No modifier stops this: `transfer` only checks the *sender's* unlocked balance (`_revertIfLocked`), the recipient check is just `recipient_ != address(0)`, and `addToDepositTokensOfAccount` has no opt-out, dust threshold, or per-token cap.

### Impact Explanation
- Unprivileged denial of service on a per-account basis: the victim is barred from adding any new collateral or debt position, and cannot receive any new `msd*` token.
- Fund impact: a victim who becomes liquidatable cannot cure via a new collateral type; for a victim contract (e.g., a SmartFarming-managed position or a multisig wrapper that can hold but not `transfer`/`withdraw` arbitrary dust `msd*`), the dust entries may be effectively un-removable, turning the DoS into a permanent freeze of the deposit/receive path.
- Cost asymmetry: the attacker spends ~30 dust transfers once; the victim must locate and clear each foreign token entry (each removal requires a transfer whose balance hits exactly zero) before regaining functionality.

### Likelihood Explanation
- Reachability: `DepositToken.transfer` is fully public and dust requires only 1 wei of each `msd*` per slot; `seize`/`deposit on behalf of` give additional forced-add vectors.
- Preconditions that bound it: the attack needs `30 - len(victim's tokens)` distinct *registered* deposit tokens to exist in the pool. If a deployed pool lists fewer deposit tokens than the free slots, the attacker cannot fill the list (debt tokens can't be force-added). Severity therefore scales with the number of listed collaterals; on pools with ≥ ~15–20 deposit tokens the attack is cheap and reliable.
- The victim can clear entries themselves by transferring out the dust (if they can transact and the dust is unlocked), which caps the impact at temporary freezing/liquidation-griefing for plain EOAs, and permanent for contracts without a generic ERC20 escape hatch.

### Recommendation
- Make `addToDepositTokensOfAccount`/`addToDebtTokensOfAccount` opt-in or consent-based for unsolicited transfers (e.g., only auto-add on `deposit`/`mint` initiated through `Pool`, not on plain `transfer`), or
- enforce a minimum first-transfer amount (dust threshold) so filling 30 slots costs real capital, or
- allow anyone to call a permissionless `removeFromDepositTokensOfAccount(account, token)` for entries with zero/insignificant balance so a victim contract can't be bricked.

### Proof of Concept
Hardhat sketch against the existing fixture style in `test/Pool.test.ts` (uses real `Pool` + real `DepositToken`s; assumes the pool under test registers ≥ N deposit tokens):

```ts
// attacker fills victim's depositTokensOfAccount to MAX_TOKENS_PER_USER
const max = (await pool.MAX_TOKENS_PER_USER()).toNumber(); // 30
const existing = (await pool.getDepositTokensOfAccount(victim.address)).length
             + (await pool.getDebtTokensOfAccount(victim.address)).length;

for (const msd of allDepositTokens.slice(0, max - existing)) {
  // attacker holds msd dust (deposited earlier)
  await msd.connect(attacker).transfer(victim.address, 1);
}

expect(await pool.getDepositTokensOfAccount(victim.address)).to.have.lengthOf(max - debtLen);

// victim tries to deposit a collateral type not yet in their list -> reverts
await expect(
  newDepositToken.connect(victim).deposit(victim.address, parseEther('1'))
).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');

// and any incoming transfer of a new msd* reverts
await expect(
  anotherMsd.connect(attacker).transfer(victim.address, 1)
).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');
```

Fork note: on a fork of a deployed pool, enumerate `pool.getDepositTokens()`; if its length ≥ free slots of the target, the PoC executes verbatim with `impersonated`/`deal`'d `msd*` dust. If the pool lists fewer deposit tokens than free slots, the attack only consumes (not fills) the list and the finding degrades to no-impact for that deployment.

### Citations

**File:** contracts/Pool.sol (L143-148)
```text
    modifier onlyIfAdditionWillNotReachMaxTokens(address account_) {
        if (debtTokensOfAccount.length(account_) + depositTokensOfAccount.length(account_) >= MAX_TOKENS_PER_USER) {
            revert UserReachedMaxTokens();
        }
        _;
    }
```

**File:** contracts/Pool.sol (L216-220)
```text
    function addToDepositTokensOfAccount(address account_) external onlyIfAdditionWillNotReachMaxTokens(account_) {
        address _depositToken = _msgSender();
        _revertIfSenderIsNotDepositToken(_depositToken);
        if (!depositTokensOfAccount.add(account_, _depositToken)) revert DepositTokenAlreadyExists();
    }
```

**File:** contracts/Pool.sol (L587-593)
```text
        syntheticToken_.burn(_msgSender, amountToRepay_);
        _debtToken.burn(account_, amountToRepay_);
        depositToken_.seize(account_, _msgSender, _toLiquidator);

        if (_fee > 0) {
            depositToken_.seize(account_, _poolRegistry.feeCollector(), _fee);
        }
```

**File:** contracts/DepositToken.sol (L485-488)
```text
        // Add this token to the deposit tokens list if the recipient is receiving it for the 1st time
        if (_balanceBefore == 0 && amount_ > 0) {
            pool.addToDepositTokensOfAccount(account_);
        }
```

**File:** contracts/DepositToken.sol (L517-525)
```text
        // Add this token to the deposit tokens list if the recipient is receiving it for the 1st time
        if (_recipientBalanceBefore == 0 && amount_ > 0) {
            pool.addToDepositTokensOfAccount(recipient_);
        }

        // Remove this token from the deposit tokens list if the sender's balance goes to zero
        if (amount_ > 0 && balanceOf[sender_] == 0) {
            pool.removeFromDepositTokensOfAccount(sender_);
        }
```
