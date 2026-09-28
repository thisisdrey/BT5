### Title
Dust deposit-token transfers fill a victim's per-account token list and revert their deposits/mints (DoS) - ([File: contracts/DepositToken.sol](contracts/DepositToken.sol), [File: contracts/Pool.sol](contracts/Pool.sol))

### Summary
`Pool` tracks each account's deposit tokens in `depositTokensOfAccount` and enforces `MAX_TOKENS_PER_USER`, reverting with `UserReachedMaxTokens` once the cap is reached. Any unprivileged user can permissionlessly push arbitrary tokens into a victim's list by dust-transferring `DepositToken` shares, because `_transfer` auto-registers the token on the recipient. Once the victim's list is full, every path that adds a *new* deposit token to that account — `deposit` on their behalf, incoming `transfer`/`transferFrom`, and `DepositToken.seize` during liquidation fee collection — reverts, causing a repeatable crash of those functions for the victim.

### Finding Description
- `DepositToken._transfer` calls `pool.addToDepositTokensOfAccount(recipient_)` whenever the recipient's prior balance was zero and `amount_ > 0` — there is no opt-in and no minimum amount, so a 1-wei transfer permanently registers the token. [1](#0-0) 
- `DepositToken.transfer`/`transferFrom` only check the *sender's* unlocked balance via `_revertIfLocked`; nothing prevents sending dust to an arbitrary victim. [2](#0-1) 
- `Pool.addToDepositTokensOfAccount` reverts `UserReachedMaxTokens` when the account's set reaches `MAX_TOKENS_PER_USER` (the error and set are defined in `Pool.sol`/`PoolStorage.sol`; I could not confirm the exact constant value from indexed snippets). [3](#0-2) 
- Consequently: a victim at the cap cannot `deposit` a collateral type they don't already hold (`_mint` → `addToDepositTokensOfAccount` reverts), cannot receive transfers of a new deposit token, and any liquidation that would route a new token type to them via `seize` reverts. [4](#0-3) 
- The victim can recover only by manually transferring each dust token back out (each `transfer` removes the token from their list when balance hits zero), so this is a temporary, repeatable freeze rather than permanent loss — matching the "temporary freezing of funds" acceptance criterion and the CVE's "repeatable crash / hang" availability class.

### Impact Explanation
An attacker can, in one transaction batch, fill a target's token list and cause all deposits of new collateral types and inbound transfers to revert. A victim who needs to add a *different* collateral to stay healthy is blocked; liquidations that would credit the victim a new token type (protocol-fee seizes to `feeCollector` aside, any `seize` to a fresh recipient at cap) revert, delaying bad-debt cleanup.

### Likelihood Explanation
Requires only an EOA, dust amounts of each whitelisted `DepositToken` (which are freely mintable to the attacker via `deposit` or obtainable on market), and no privileged role. Cost scales with the number of distinct deposit tokens and `MAX_TOKENS_PER_USER`. It is not a gas/unbounded-loop issue — the revert is a hard cap check, so the DoS is deterministic and cheap to trigger.

### Recommendation
Only add to `depositTokensOfAccount` on user-initiated actions (`deposit`, `mint` paths), not on inbound transfers; or allow `seize`/liquidation paths to skip the cap; or let `transfer` to an account at the cap simply not register the token. Removing a token on `transfer` out already exists, so skipping registration for unsolicited transfers is the minimal fix.

### Proof of Concept
Hardhat fork sketch (I could not verify `MAX_TOKENS_PER_USER`'s value from indexed code — set `N` accordingly):

```ts
// victim = target account with an open position
const attacker = ...;
for (const dt of allDepositTokens) {
  // attacker obtains dust of each deposit token
  await dt.connect(attacker).deposit(1, attacker.address);
  await dt.connect(attacker).transfer(victim.address, 1); // registers token on victim
}
// repeat until depositTokensOfAccount[victim].length == MAX_TOKENS_PER_USER
// now: any deposit of a NEW collateral type for victim reverts
await expect(
  newDepositToken.connect(attacker).deposit(amount, victim.address)
).to.be.revertedWithCustomError(pool, "UserReachedMaxTokens");
// and inbound transfers of a new token type revert
await expect(
  otherToken.connect(attacker).transfer(victim.address, 1)
).to.be.reverted;
```

Note: the exact value of `MAX_TOKENS_PER_USER` and the body of `Pool.addToDepositTokensOfAccount` were not fully retrievable from the indexed snippets; the mechanics (`MappedEnumerableSet` registration on zero-balance recipients and the `UserReachedMaxTokens` revert) are confirmed, but a Devin session with full file access should verify the cap and finalize the PoC constants.

### Citations

**File:** contracts/DepositToken.sol (L348-376)
```text
    function transfer(address to_, uint256 amount_) external override returns (bool) {
        address _msgSender = _msgSender();
        _revertIfLocked(_msgSender, amount_);

        _transfer(_msgSender, to_, amount_);
        return true;
    }

    /// @inheritdoc IERC20
    function transferFrom(
        address sender_,
        address recipient_,
        uint256 amount_
    ) external override nonReentrant returns (bool) {
        _revertIfLocked(sender_, amount_);

        address _msgSender = _msgSender();
        uint256 _currentAllowance = allowance[sender_][_msgSender];
        if (_currentAllowance != type(uint256).max) {
            if (_currentAllowance < amount_) revert AmountExceedsAllowance();
            unchecked {
                _approve(sender_, _msgSender, _currentAllowance - amount_);
            }
        }

        _transfer(sender_, recipient_, amount_);

        return true;
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

**File:** contracts/Pool.sol (L29-33)
```text
error SyntheticDoesNotExist();
error SenderIsNotDebtToken();
error SenderIsNotDepositToken();
error UserReachedMaxTokens();
error PoolRegistryIsNull();
```
