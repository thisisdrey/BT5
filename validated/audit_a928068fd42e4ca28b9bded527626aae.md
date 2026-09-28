### Title
Anyone can force-fill a victim's per-account token lists via dust `DepositToken` transfers, blocking the victim from holding new collateral or debt tokens - ([File: contracts/DepositToken.sol](contracts/DepositToken.sol))

### Summary
The Phi finding is about publicly-callable bookkeeping functions (`_addCredIdPerAddress`/`_removeCredIdPerAddress`) that let anyone arbitrarily grow/reorder a victim's per-account ID array, griefing holders. Metronome does not expose its bookkeeping functions directly — `Pool.addToDepositTokensOfAccount` / `Pool.removeFromDepositTokensOfAccount` verify `msg.sender` is a registered token — but the same effect is reachable through the public ERC20 surface: `DepositToken.transfer`/`transferFrom` adds the token to the *recipient's* `depositTokensOfAccount` set whenever the recipient's balance goes `0 → nonzero`, and the recipient cannot prevent this. Combined with `MAX_TOKENS_PER_USER` and the swap-and-pop ordering in `MappedEnumerableSet`, an unprivileged attacker can fill a victim's list with dust shares and make every subsequent "new token" action of the victim revert.

### Finding Description
`DepositToken._transfer` unconditionally calls `pool.addToDepositTokensOfAccount(recipient_)` when the recipient's prior balance is zero. [1](#0-0)  `Pool.addToDepositTokensOfAccount` is gated by `onlyIfAdditionWillNotReachMaxTokens`, which reverts once `debtTokensOfAccount.length + depositTokensOfAccount.length >= MAX_TOKENS_PER_USER`. [2](#0-1)  The same add-on-first-balance / remove-on-zero-balance logic exists on mint (`_mint` → `addToDepositTokensOfAccount`) [3](#0-2) , burn/transfer-out [4](#0-3) , and symmetrically for `DebtToken` (`addToDebtTokensOfAccount`/`removeFromDebtTokensOfAccount`, caller-checked to be a real debt token). [5](#0-4) 

Attack path:

1. Attacker deposits a dust amount of collateral into `DepositToken.deposit` for every registered deposit token (bounded only by governor-registered tokens), receiving 1-wei msdTOKEN balances.
2. Attacker calls `transfer(victim, 1)` on each deposit token. Each call pushes a new entry into `depositTokensOfAccount[victim]` via `Pool.addToDepositTokensOfAccount`.
3. Once the victim's combined deposit+debt list length reaches `MAX_TOKENS_PER_USER`, any victim action that would add a *new* token — `deposit` into a collateral they don't already hold, `DebtToken.issue`/`mint` of a synth they don't already owe, or receiving a dusted token — reverts with `UserReachedMaxTokens`, because `_mint`/issue internally hits the same `onlyIfAdditionWillNotReachMaxTokens` gate.

This is the direct analog: like Phi's public `_addCredIdPerAddress`, an unprivileged user can force unconsented entries into another account's per-account list and thereby DoS that account's new positions. Ordering griefing also exists structurally — `MappedEnumerableSet._remove` uses swap-and-pop [6](#0-5)  — though unlike Phi's `WrongCredId` revert, removal here is index-map-based so reordering alone doesn't brick removal; the reachable damage is the list-bloat/cap DoS.

### Impact Explanation
Temporary freezing/liveness impact: the victim is blocked from depositing any *new* collateral type and from issuing any *new* debt token while their list is saturated. Existing balances still work (the gate only fires on `0 → nonzero` transitions), so funds aren't stolen, but a user mid-action (e.g., trying to deposit a new collateral to stay healthy before liquidation, or a new user trying to open a first position after being dusted) can be denied service. Remediation costs the victim transactions (transfer each dust token out so `removeFromDepositTokensOfAccount` clears the entry [7](#0-6) ), and the attacker can re-dust via front-running, mirroring the "remove then re-add" griefing loop in the Phi report.

### Likelihood Explanation
Requires only an EOA: dust deposits plus `transfer` calls; gas is cheap on the deployed L2 chains (Base/Optimism). The ceiling on impact is the number of registered deposit tokens, which is governor-controlled — so saturation is only achievable while `registered deposit tokens >= MAX_TOKENS_PER_USER - victim's debt entries`, or by splitting the victim's debt list too (attacker can't force debt entries; only deposit tokens are force-pushable). This makes the attack real but narrower than the Phi primitive — it cannot grow the list with non-existent tokens, only registered ones — so severity is bounded by token count vs. `MAX_TOKENS_PER_USER`. No modifier stops it: `transfer` only checks `_revertIfLocked` on the *sender* [8](#0-7)  and there is no opt-out for unsolicited list entries.

### Recommendation
Decouple list membership from push-style transfers, or make saturation non-fatal: e.g., skip `addToDepositTokensOfAccount` (or let it silently no-op rather than revert) when the recipient is at the cap, so an attacker dusting a capped victim doesn't brick the *attacker's* transfer but the victim's deposit of a genuinely new token can still succeed; alternatively, allow users to remove entries without holding a balance and/or compute per-account positions without relying on the enumerable set (iterate `depositTokens` and check balances) so the cap becomes unnecessary.

### Proof of Concept
```solidity
// Hardhat/Foundry fork sketch against a deployed Pool
// Pre: victim has few/no tokens; N = MAX_TOKENS_PER_USER - victim debt entries
// Attacker holds dust in each registered DepositToken:
for (uint i; i < depositTokens.length && i < N; ++i) {
    IERC20 dToken = pool.depositTokens()[i];       // registered deposit token
    IERC20(dToken.underlying()).approve(dToken, 1);
    dToken.deposit(1);                             // attacker mints dust share
    dToken.transfer(victim, 1);                    // -> pool.addToDepositTokensOfAccount(victim)
}
assertEq(pool.getDepositTokensOfAccount(victim).length, N);

// Victim now tries to deposit a collateral they don't already hold:
vm.prank(victim);
newDepositToken.deposit(100e18); // reverts: UserReachedMaxTokens
// Same for DebtToken.issue of a synth the victim doesn't owe yet.
```
A fork PoC needs at least `MAX_TOKENS_PER_USER` registered deposit tokens (or a victim whose debt-token entries consume the rest of the cap) to reach saturation; on deployments where the token count is below the cap, the attack still works partially — forcing unremovable-by-consent list entries and gas costs on the victim — but the hard DoS only triggers once the cap is reached.

### Citations

**File:** contracts/DepositToken.sol (L348-353)
```text
    function transfer(address to_, uint256 amount_) external override returns (bool) {
        address _msgSender = _msgSender();
        _revertIfLocked(_msgSender, amount_);

        _transfer(_msgSender, to_, amount_);
        return true;
```

**File:** contracts/DepositToken.sol (L485-488)
```text
        // Add this token to the deposit tokens list if the recipient is receiving it for the 1st time
        if (_balanceBefore == 0 && amount_ > 0) {
            pool.addToDepositTokensOfAccount(account_);
        }
```

**File:** contracts/DepositToken.sol (L517-520)
```text
        // Add this token to the deposit tokens list if the recipient is receiving it for the 1st time
        if (_recipientBalanceBefore == 0 && amount_ > 0) {
            pool.addToDepositTokensOfAccount(recipient_);
        }
```

**File:** contracts/DepositToken.sol (L522-525)
```text
        // Remove this token from the deposit tokens list if the sender's balance goes to zero
        if (amount_ > 0 && balanceOf[sender_] == 0) {
            pool.removeFromDepositTokensOfAccount(sender_);
        }
```

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

**File:** contracts/Pool.sol (L630-634)
```text
    function removeFromDepositTokensOfAccount(address account_) external {
        address _depositToken = _msgSender();
        _revertIfSenderIsNotDepositToken(_depositToken);
        if (!depositTokensOfAccount.remove(account_, _depositToken)) revert DepositTokenDoesNotExist();
    }
```

**File:** contracts/lib/MappedEnumerableSet.sol (L44-60)
```text
            uint256 toDeleteIndex = valueIndex - 1;
            uint256 lastIndex = set._ofAddress[_key]._values.length - 1;

            if (lastIndex != toDeleteIndex) {
                address lastvalue = set._ofAddress[_key]._values[lastIndex];

                // Move the last value to the index where the value to delete is
                set._ofAddress[_key]._values[toDeleteIndex] = lastvalue;
                // Update the index for the moved value
                set._ofAddress[_key]._indexes[lastvalue] = valueIndex; // Replace lastvalue's index to valueIndex
            }

            // Delete the slot where the moved value was stored
            set._ofAddress[_key]._values.pop();

            // Delete the index for the deleted slot
            delete set._ofAddress[_key]._indexes[value];
```
