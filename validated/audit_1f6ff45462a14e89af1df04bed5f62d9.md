### Title
Dust-transfer griefing fills victim's per-account token list, permanently blocking new deposits/mints via `UserReachedMaxTokens` reverts - (File: contracts/Pool.sol)

### Summary
`Pool` tracks a per-account bounded list of debt tokens and deposit tokens (`debtTokensOfAccount`, `depositTokensOfAccount`) capped at `MAX_TOKENS_PER_USER`. Any `DepositToken._transfer` to a recipient with zero balance unconditionally calls `pool.addToDepositTokensOfAccount(recipient)`, which reverts once the combined count reaches the cap. An unprivileged attacker can deposit minimal collateral into every whitelisted deposit token and dust-transfer 1 wei of each `msdToken` to a victim, filling the victim's list. From then on, every action that would add a token to the victim's account — receiving any deposit token they don't already hold, depositing a new collateral type, minting a synthetic whose debt token they don't yet hold — reverts with `UserReachedMaxTokens`. [1](#0-0) [2](#0-1) [3](#0-2) 

### Finding Description
1. `DepositToken.transfer`/`transferFrom`/`seize` all funnel into `_transfer`, which calls `pool.addToDepositTokensOfAccount(recipient_)` whenever the recipient's prior balance was 0 (`DepositToken.sol:517-520`). There is no opt-out and no minimum amount.
2. `Pool.addToDepositTokensOfAccount` enforces `onlyIfAdditionWillNotReachMaxTokens`, reverting when `debtTokensOfAccount.length + depositTokensOfAccount.length >= MAX_TOKENS_PER_USER` (`Pool.sol:143-148, 216-220`). The same modifier guards `addToDebtTokensOfAccount` (`Pool.sol:204-208`).
3. The attacker only needs to hold each whitelisted deposit token once: deposit a tiny amount (or obtain via liquidation/swap), keep the unlocked portion, then `msdX.transfer(victim, 1)` for every deposit token in the pool. Sender-side `_revertIfLocked` only constrains the *sender's* unlocked balance — the recipient's consent is never required (`DepositToken.sol:348-354`).
4. Once `depositTokensOfAccount(victim).length + debtTokensOfAccount(victim).length == MAX_TOKENS_PER_USER`, any subsequent `_transfer`/`_mint`/DebtToken issue that would add a new entry to the victim reverts inside `addTo*TokensOfAccount`, bricking those code paths for the victim.
5. Recovery is asymmetric: the victim can only free a slot by zeroing a balance (transferring dust out or repaying debt in full). If the victim's position is underwater, `unlockedBalanceOf` returns 0 (`DepositToken.sol:383-397`), so the dusted tokens are locked by `_revertIfLocked` and cannot be removed — the victim cannot even clear the attacker-controlled entries, and cannot deposit new collateral or mint to restore health while liquidation proceeds against them.

### Impact Explanation
Permanent-to-long-duration denial of core protocol functions for targeted users, matching the CVE's DoS bug class (liveness invariant). Concretely: the victim cannot (a) receive any deposit token they don't already hold — all inbound transfers revert; (b) open a position with a new collateral type; (c) mint any synthetic asset whose debt token isn't already in their list. For an underwater victim this is worse: the dust is locked and unremovable, and the victim is prevented from adding collateral of a *new* type to rescue the position while `Pool.liquidate` (which is not paused-gated and uses `seize`, bypassing the lock check) remains fully usable against them — a forced-liquidation path that amounts to freezing the victim's ability to act while their funds are drained.

### Likelihood Explanation
Cost is low: one dust transfer per whitelisted deposit token (the offering list is small and governor-bounded). No privileged role, oracle manipulation, or timing is needed — the attack uses only public `DepositToken.transfer` reachable by any EOA or via `Operator.execute`. Griefing cost is nonzero (attacker must first acquire each msdToken), which caps how many slots can be filled, but reaching `MAX_TOKENS_PER_USER` is bounded by the number of pool offerings. Impact is griefing/DoS rather than direct theft; it becomes loss-adjacent when it prevents an underwater victim from topping up collateral, though a victim holding existing collateral types can still deposit those (no list-add required).

### Recommendation
Apply the max-tokens check only to actions the account initiates, not to passive receipt. Options:
- Remove `onlyIfAdditionWillNotReachMaxTokens` from `addToDepositTokensOfAccount` (inbound transfers/mints shouldn't be capped), or let it silently skip/push only for account-initiated `deposit`/`issue` paths.
- Alternatively, gate the list-add in `_transfer`/`seize` on the recipient not being at cap and silently no-op (token balance still accrues without list membership, and `debtPositionOf` iterates the list — so this option would misprice collateral and is *not* recommended; prefer keeping the list authoritative but uncapped for inbound flows).
- Add a per-account "claim/sweep" function letting users force-remove a deposit token entry while forfeiting or redirecting dust, so locked dust can always be evicted.

### Proof of Concept
Hardhat fork sketch:

```ts
// setup: pool with N whitelisted DepositTokens, victim with a position
const max = await pool.MAX_TOKENS_PER_USER();
const depositTokens = await pool.getDepositTokens(); // whitelisted collaterals

// Attacker deposits dust collateral in each deposit token to obtain msdTokens
for (const dt of depositTokens) {
  const dep = await ethers.getContractAt('DepositToken', dt);
  const underlying = await ethers.getContractAt('ERC20', await dep.underlying());
  await underlying.connect(attacker).approve(dep.address, DUST);
  await dep.connect(attacker).deposit(DUST, attacker.address);
}

// Victim already has K debt tokens; fill remaining slots
for (const dt of depositTokens.slice(0, max - K - /*victim existing deposits*/)) {
  await (await ethers.getContractAt('DepositToken', dt)).connect(attacker).transfer(victim.address, 1);
}

// Assert: victim now at cap
expect(await pool.debtTokensOfAccountLength(victim) + await pool.depositTokensOfAccountLength(victim)).to.eq(max);

// 1) Any inbound transfer of a token victim doesn't hold reverts
await expect(depositTokens[maxIdx].connect(attacker).transfer(victim.address, 1))
  .to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');

// 2) Victim cannot deposit a new collateral type (mint -> addToDepositTokensOfAccount reverts)
await expect(newDepositToken.connect(victim).deposit(AMT, victim.address))
  .to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');

// 3) Victim cannot mint a synthetic whose DebtToken is new to them
await expect(debtToken.connect(victim).issue(MINT_AMT, victim.address))
  .to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');

// 4) If victim is underwater, dust is locked: transfer out reverts with NotEnoughFreeBalance
await masterOracle.updatePrice(collateral, LOW_PRICE);
await expect(depositTokens[0].connect(victim).transfer(attacker.address, 1))
  .to.be.revertedWithCustomError(depositTokens[0], 'NotEnoughFreeBalance');
```

*Uncertainty: the exact value of `MAX_TOKENS_PER_USER` and whether the whitelisted offering count alone suffices to fill it depends on deployment config; if offerings < cap, the attacker additionally needs the victim's own debt tokens to occupy slots, which limits reachability but does not eliminate it.*

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

**File:** contracts/Pool.sol (L204-220)
```text
    function addToDebtTokensOfAccount(address account_) external onlyIfAdditionWillNotReachMaxTokens(account_) {
        address _debtToken = _msgSender();
        _revertIfSenderIsNotDebtToken(_debtToken);
        if (!debtTokensOfAccount.add(account_, _debtToken)) revert DebtTokenAlreadyExists();
    }

    /**
     * @notice Add a deposit token to the per-account list
     * @dev This function is called from `DepositToken` when user's balance changes from `0`
     * @dev The caller should ensure to not pass `address(0)` as `_account`
     * @param account_ The account address
     */
    function addToDepositTokensOfAccount(address account_) external onlyIfAdditionWillNotReachMaxTokens(account_) {
        address _depositToken = _msgSender();
        _revertIfSenderIsNotDepositToken(_depositToken);
        if (!depositTokensOfAccount.add(account_, _depositToken)) revert DepositTokenAlreadyExists();
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
