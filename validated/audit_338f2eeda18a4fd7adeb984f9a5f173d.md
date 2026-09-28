### Title
Unprivileged dust-transfer griefing fills victim's per-account token list to `MAX_TOKENS_PER_USER`, DoS-ing new deposits and new debt issuance - (File: contracts/Pool.sol)

### Summary
`Pool` tracks per-account deposit/debt token lists in `MappedEnumerableSet.AddressSet` (`depositTokensOfAccount`, `debtTokensOfAccount`) and enforces a hard cap of `MAX_TOKENS_PER_USER = 30` across both lists combined [1](#0-0) . `DepositToken._transfer` and `DepositToken._mint` unconditionally call `pool.addToDepositTokensOfAccount(recipient_)` whenever the recipient's balance goes 0 → >0 [2](#0-1) . Because `msdTOKEN` is a freely transferable ERC-20 (`transfer`/`transferFrom` are public, only gated by `_revertIfLocked`), an attacker can dust-transfer 1 wei of every listed deposit token to a victim, filling their list to the cap. After that, every code path that would add a *new* token entry for the victim reverts with `UserReachedMaxTokens` — `DepositToken.deposit` (mint to a fresh collateral), `DebtToken.issue`/`mint` (mint of a debt token the victim doesn't already hold, via `addToDebtTokensOfAccount` in `_mint`) [3](#0-2) , and plain `transfer`/`transferFrom`/`seize` of any msdTOKEN the victim doesn't yet hold.

### Finding Description
- `addToDepositTokensOfAccount` / `addToDebtTokensOfAccount` are callable only by registered tokens (`_revertIfSenderIsNotDepositToken`/`DebtToken`), but *any holder of a listed deposit token can trigger the add on behalf of an arbitrary recipient* simply by transferring dust — there is no opt-in or allowlist.
- The modifier `onlyIfAdditionWillNotReachMaxTokens` reverts when `debtTokensOfAccount.length + depositTokensOfAccount.length >= 30`, and it guards the add functions, which are invoked inline inside `_transfer`, `_mint` (deposit), and `_mint` (issue/mint). The revert therefore bubbles up and reverts the whole user operation.
- Reachability: attacker calls `depositToken.transfer(victim, 1)` for each of the pool's deposit tokens (up to 30 slots; the pool itself caps deposit tokens at `MAX_TOKENS_PER_USER` in `addDepositToken` [4](#0-3) ). Cost is ~1 wei of each collateral plus gas. No privileged role, flash loan, or oracle manipulation needed.
- The check combines *both* lists, so slots can also be consumed via `DebtToken._mint` → `addToDebtTokensOfAccount` (only through `issue`/`mint` by SmartFarmingManager — the victim's own position counts against them, which is by design, but attacker-controlled dust fills the rest).

### Impact Explanation
- Denial of service against a specific victim: the victim cannot deposit any *new* collateral type and cannot issue any *new* synthetic debt type until they manually transfer out the dust positions to shrink their list. This is a temporary freezing of the victim's ability to manage/open positions (an accepted impact class), analogous to the crash-DoS class of the hint CVE.
- Concretely harmful scenario: a victim near liquidation who wants to top up with a collateral type they don't already hold is blocked and becomes liquidatable; every rescue deposit reverts with `UserReachedMaxTokens`.
- Existing holdings are not frozen: `withdraw`, `repay`, `repayAll`, and `transfer` of already-held tokens still work (removals don't hit the cap check). The victim can self-recover by sending the dust back out, so this is temporary — hence medium/low severity, matching the "DoS" (not theft) nature of the reference report.

### Likelihood Explanation
- Attack cost is trivial (dust amounts of already-listed collateral tokens; attacker can obtain them by depositing minimum amounts themselves). No privileged actor required — satisfies the unprivileged-attacker constraint.
- Effectiveness depends on the pool listing enough deposit + debt tokens to approach 30; the pool's own cap (`depositTokens.length() >= MAX_TOKENS_PER_USER`) makes saturation possible only when the sum of registered deposit tokens plus debt tokens reaches 30. On a fully populated pool the attack is fully deterministic and reproducible in a fork test.

### Recommendation
- Make the per-account list add opt-in or tolerant: e.g., only add to `depositTokensOfAccount` on `deposit`/`_mint`, not on incoming `_transfer`, or track an explicit `useAsCollateral` flag instead of inferring it from `balance > 0`.
- Alternatively, let `addToDepositTokensOfAccount` silently skip (return false) instead of reverting when the cap is reached, since the list is only used for position enumeration in `depositOf`/`debtPositionOf`.
- If transfers must register tokens, charge a minimum-transfer dust threshold.

### Proof of Concept
Reproducible Hardhat fork test (mainnet `Pool`, `DepositToken` from `deployments/mainnet/`):

```ts
// Fork mainnet; impersonate attacker EOA holding small amounts of each msdTOKEN.
const MAX = await pool.MAX_TOKENS_PER_USER(); // 30
const depositTokens = await pool.getDepositTokens(); // all listed msdTOKENs

// 1) Attacker deposits 1 wei of each underlying to get msdTOKEN balance, then dusts victim.
for (const dt of depositTokens) {
  const token = await ethers.getContractAt('DepositToken', dt);
  await token.connect(attacker).deposit(1, attacker.address); // or transfer existing balance
  await token.connect(attacker).transfer(victim.address, 1);  // fills victim's list
}
// victim now has depositTokensOfAccount.length == MAX (assuming debt side empty, repeat via own positions if needed)

// 2) Any new deposit or issue by/for the victim now reverts.
await expect(
  someNewDepositToken.connect(victim).deposit(amount, victim.address)
).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');

await expect(
  debtToken.connect(victim).issue(amount, victim.address)
).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');

// 3) Victim's existing withdraw still works, confirming temporary (not permanent) freeze:
await depositToken0.connect(victim).withdraw(await depositToken0.unlockedBalanceOf(victim.address), victim.address);
```

Preconditions: pool's registered deposit+debt token count must allow reaching 30 (cap enforced symmetrically in `addDepositToken`), which holds on fully populated deployments; otherwise the attacker can only partially fill the list and the DoS is proportionally weaker.

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

**File:** contracts/Pool.sol (L698-710)
```text
    function addDepositToken(address depositToken_) external onlyGovernor {
        if (depositToken_ == address(0)) revert AddressIsNull();
        IERC20 _underlying = IDepositToken(depositToken_).underlying();
        if (address(depositTokenOf[_underlying]) != address(0)) revert UnderlyingAssetInUse();
        // Note: Fee collector collects deposit tokens as fee
        if (depositTokens.length() >= MAX_TOKENS_PER_USER) revert ReachedMaxDepositTokens();

        if (!depositTokens.add(depositToken_)) revert DepositTokenAlreadyExists();

        depositTokenOf[_underlying] = IDepositToken(depositToken_);

        emit DepositTokenAdded(depositToken_);
    }
```

**File:** contracts/DepositToken.sol (L517-520)
```text
        // Add this token to the deposit tokens list if the recipient is receiving it for the 1st time
        if (_recipientBalanceBefore == 0 && amount_ > 0) {
            pool.addToDepositTokensOfAccount(recipient_);
        }
```

**File:** contracts/DebtToken.sol (L597-600)
```text
        //  Add this token to the debt tokens list if the recipient is receiving it for the 1st time
        if (_balanceBefore == 0 && amount_ > 0) {
            pool.addToDebtTokensOfAccount(account_);
        }
```
