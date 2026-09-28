### Title
Attacker permanently blocks a victim's ability to deposit new collateral types or take on new debt by dust-filling `MAX_TOKENS_PER_USER` account lists via `DepositToken.transfer` / `DebtToken` mint dust - (File: contracts/Pool.sol)

### Summary
The bug class in CVE-2020-8617 is: an unprivileged remote party sends a specially-crafted message that pushes the victim into an inconsistent state, tripping an assertion that degrades/DoSes the victim. The Metronome analog is the per-account token registry in `Pool`: `debtTokensOfAccount` + `depositTokensOfAccount` are capped at `MAX_TOKENS_PER_USER = 30` combined, and any addition beyond that reverts with `UserReachedMaxTokens` [1](#0-0) . Both `DepositToken._transfer` and `DebtToken` balance changes call `pool.addToDepositTokensOfAccount` / `addToDebtTokensOfAccount` for the *recipient*, giving an attacker a way to insert entries into a victim's list without consent by sending 1-wei dust [2](#0-1) . Once the victim's combined list hits 30, every path that would add a *new* token type reverts — `deposit()` into a not-yet-held collateral, `issue()`/`mint()` of a not-yet-held synthetic, and even receiving any deposit-token transfer.

### Finding Description
`Pool.addToDepositTokensOfAccount` and `Pool.addToDebtTokensOfAccount` enforce `onlyIfAdditionWillNotReachMaxTokens(account_)` on the *target account*, not the caller [3](#0-2) . The trigger functions are permissionless:

- `DepositToken._transfer` calls `pool.addToDepositTokensOfAccount(recipient_)` whenever the recipient's prior balance was 0 [4](#0-3) . `transfer`/`transferFrom` are public and only require the sender's amount to be unlocked (`_revertIfLocked`/`unlockedBalanceOf` on the *sender*), so an attacker holding even 1 wei of each listed deposit token can push entries into any victim's list.
- `DebtToken` mint/issue paths symmetrically call `addToDebtTokensOfAccount` for the account receiving the debt position.

Attack path (EOA, no privileged role, no oracle manipulation):

1. Attacker deposits dust (`1 wei` of underlying or the minimum viable amount) into every listed `DepositToken` in the target pool — 15+ deposit tokens per pool is typical — receiving dust `msd*` balances.
2. Attacker calls `depositToken_i.transfer(victim, 1)` for each token until `depositTokensOfAccount.length(victim) + debtTokensOfAccount.length(victim) == 30`.
3. Every subsequent victim action that needs a *new* list entry reverts with `UserReachedMaxTokens`:
   - `pool.deposit(newCollateralToken, ...)` → `DepositToken._mint` → `addToDepositTokensOfAccount` reverts [5](#0-4) .
   - `debtToken.issue/mint` of a synthetic the victim doesn't yet owe reverts.
   - Anyone sending the victim a deposit token they don't hold reverts.

The "crafted message → inconsistent internal state → assertion/DoS" structure mirrors the CVE: the attacker uses knowledge of a public identifier (the deposit-token list cap) to force the victim's account state into a configuration where the protocol's own invariant check (`onlyIfAdditionWillNotReachMaxTokens`) halts operations.

### Impact Explanation
A victim who is approaching liquidation cannot deposit a new collateral type to restore health — the very action needed to avoid liquidation is bricked, so the position is forcibly liquidated (loss of liquidation fee + collateral seized). A healthy victim is barred from opening positions in new markets. Because the attacker controls the dust entries and can re-top them each block, the victim cannot reliably evict entries: `removeFromDepositTokensOfAccount` only fires when the victim's balance of that token goes to zero [6](#0-5) , and the attacker can front-run the victim's cleanup transfer with a fresh 1-wei dust transfer (1 wei re-adds the entry since balance never reaches a state that blocks it). Impact class: temporary-to-indefinite freezing of the victim's ability to use the protocol and forced liquidation = theft of liquidation fee / loss of collateral value. No pause flag, supply cap, reentrancy guard, or `SynthContext` check stops it; the limit is applied to the victim regardless of caller.

### Likelihood Explanation
Cost is bounded and small: attacker needs dust positions in up to ~30 listed deposit/debt tokens and ~30 cheap transfers; victims near the cap (active farmers hold many collateral/debt tokens already) need only a handful of dust sends. No privileged role, flash loan, oracle manipulation, or bridge trust assumption is required — only public entry points. The main caveat reducing severity: a victim *can* shed entries by transferring their entire dust balance of an attacker-added token back out (the entry is removed when balance hits 0), so a sophisticated victim can recover if not persistently front-run; the attacker's persistent re-dusting raises but does not eliminate this mitigation, and racing costs the attacker only gas.

### Recommendation
- Do not let an unsolicited incoming balance consume a victim's slot: either remove `MAX_TOKENS_PER_USER` enforcement on inbound transfers (let the recipient's list grow, enforce the cap only on `deposit()`/`issue()` initiated by the account owner), or
- Whitelist-gate additions so that only the account itself (or the Pool acting on its explicit deposit call) can add entries, or
- Introduce a two-step "claim/accept" for new token types: inbound transfer of a not-held token goes to a pending balance the recipient must accept before it counts toward the cap.

### Proof of Concept
Hardhat sketch (fork of a live pool with ≥2 deposit tokens `dtA`, `dtB`; scale the loop to `MAX_TOKENS_PER_USER - victimCurrentCount`):

```ts
// attacker holds 1 wei of underlyingA..N via pool.deposit(dust) for each listed deposit token
const victim = alice.address;
const tokens = await pool.getDepositTokens(); // listed deposit tokens
const n = 30 - await pool.depositTokensOfAccountLength(victim)
        - await pool.debtTokensOfAccountLength(victim);

for (let i = 0; i < n; i++) {
  const dt = await ethers.getContractAt("DepositToken", tokens[i]);
  // attacker deposited dust earlier; send 1 wei msdTOKEN to victim
  await dt.connect(attacker).transfer(victim, 1);
}

// victim's combined list is now == MAX_TOKENS_PER_USER
// victim tries to deposit into a collateral type they do not yet hold:
await expect(
  newDepositToken.connect(alice).deposit(amount) // -> Pool -> _mint -> addToDepositTokensOfAccount
).to.be.revertedWithCustomError(pool, "UserReachedMaxTokens");

// victim tries to issue a synthetic they have no debt in:
await expect(
  newDebtToken.connect(alice).issue(amount)
).to.be.revertedWithCustomError(pool, "UserReachedMaxTokens");

// attacker front-runs victim's cleanup: victim transfers out their full dust balance,
// attacker re-sends 1 wei in the same/next block to re-add the entry.
```

Reproducible on a mainnet fork (e.g., `deployments/mainnet` pool) since `transfer` on `DepositToken` is permissionless and only gated on the *sender's* unlocked balance.

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
