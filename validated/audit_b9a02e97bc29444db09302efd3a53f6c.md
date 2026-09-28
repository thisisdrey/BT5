### Title
Dust-transfer griefing fills `MAX_TOKENS_PER_USER` slots and blocks a victim's deposits, withdrawals of new collateral types, and new debt positions - (File: contracts/Pool.sol)

### Summary
The MsQuic report is a remote, unauthenticated crash → denial of service triggered by an attacker-crafted input (a version-negotiation packet sent to a server). The Metronome analog for "unprivileged input causes DoS" is the per-account token lists in `Pool`: `depositTokensOfAccount` and `debtTokensOfAccount` are bounded by `MAX_TOKENS_PER_USER`, and an attacker can fill a victim's list by transferring dust amounts of every `DepositToken` to them, after which any operation that needs to add a new token to the victim's list reverts.

### Finding Description
`Pool.addToDepositTokensOfAccount` and `Pool.addToDebtTokensOfAccount` share a single counter limit enforced by `onlyIfAdditionWillNotReachMaxTokens`, reverting with `UserReachedMaxTokens` once `depositTokensOfAccount.length + debtTokensOfAccount.length == MAX_TOKENS_PER_USER` for the account [1](#0-0) .

Every `DepositToken` transfer adds the token to the *recipient's* list when their prior balance is zero [2](#0-1) . `DepositToken` is a transferable ERC-20 (subject only to the sender's unlocked balance via `_revertIfLocked`), so any EOA can:

1. Deposit 1 wei of each collateral type to mint dust `DepositToken`s (or buy/transfer them).
2. `transfer(victim, 1)` for every registered deposit token.
3. Repeat across pools/debt tokens until `depositTokensOfAccount[victim].length + debtTokensOfAccount[victim].length` hits `MAX_TOKENS_PER_USER`.

From then on, any code path that calls `addToDepositTokensOfAccount(victim)` or `addToDebtTokensOfAccount(victim)` for a *new* token reverts:

- `DepositToken._deposit` / `deposit(onBehalfOf: victim)` for any collateral type the victim doesn't already hold — the entire deposit tx reverts.
- `DepositToken._mint` inside `DebtToken.issue`/Pool swap flows where a new deposit token would be recorded.
- `DebtToken._mint` → `addToDebtTokensOfAccount(victim)` — victim cannot open a debt position in a synthetic they don't already owe [3](#0-2) .
- `SmartFarmingManager.leverage` for a fresh position (mint of a new debt token for the victim) reverts.

The revert bubbles up and aborts the whole transaction — the same "request crashes the request handler" shape as the MsQuic bug.

### Impact Explanation
Temporary freezing of funds / liveness: the victim is unable to deposit collateral of any new type, receive `DepositToken` transfers of new types, or mint a new synthetic debt type until they burn the dust balance of at least one token (a full `withdraw` of the dust triggers `removeFromDepositTokensOfAccount`, freeing the slot — but the attacker can immediately re-dust). Since dusting costs ~`MAX_TOKENS_PER_USER` cheap transfers and the victim must detect and clean slots per-token, this is a persistent griefing/DoS on position management for targeted users (e.g., preventing a user from topping up collateral of a new type while their position is being pushed toward liquidation). It does not affect tokens already in the victim's lists, so it is a temporary, targetable liveness break rather than permanent loss.

### Likelihood Explanation
Fully unprivileged: requires only public `DepositToken.transfer`/`deposit` calls with attacker-owned funds. Cost scales with the number of registered deposit/debt tokens in the pool; on pools with few tokens it's cheap. No privileged role, oracle manipulation, or trusted-remote assumption needed. Partially self-mitigating (victim can free slots by withdrawing dust), which is why impact is "temporary freeze / forced position-management DoS" rather than permanent lock.

### Recommendation
- Make list admission opt-in or pool-side: only record a deposit/debt token in `*TokensOfAccount` when the account itself initiated the action (deposit on behalf of self, `issue`), not on passive receipt of a transfer; or attribute the list entry to the token balance and skip zero-balance entries during iteration instead of reverting.
- Alternatively, let `debtPositionOf`/`depositOf` iterate over *all* pool tokens filtered by nonzero balance (removing the per-account list entirely), or make `addTo*TokensOfAccount` tolerant: on reaching `MAX_TOKENS_PER_USER`, auto-evict a zero/lowest-balance entry instead of reverting.
- If the list is kept, consider excluding recipient-side additions on plain `transfer` (only add via `deposit`/`mint` paths initiated by the account).

### Proof of Concept
```solidity
// Foundry fork-style PoC against deployed Pool/DepositToken set
function test_DustGriefingBlocksNewDeposits() public {
    address victim = makeAddr("victim");

    // 1) Attacker mints dust of every deposit token and sends 1 wei of each to victim.
    address[] memory depTokens = pool.getDepositTokens();
    for (uint i; i < depTokens.length; ++i) {
        IDepositToken dt = IDepositToken(depTokens[i]);
        IERC20 underlying = IERC20(dt.underlying());
        deal(address(underlying), attacker, 1);
        underlying.approve(address(dt), 1);
        dt.deposit(1, attacker);              // mint 1 wei msdTOKEN
        dt.transfer(victim, 1);               // adds dt to victim's depositTokensOfAccount
    }
    // repeat for each debt token via issue() of dust debt to reach MAX_TOKENS_PER_USER

    // 2) Victim can no longer deposit a collateral type it doesn't already hold:
    vm.expectRevert(UserReachedMaxTokens.selector);
    vm.prank(victim);
    newDepositToken.deposit(100e18, victim);  // _mint -> addToDepositTokensOfAccount -> revert

    // 3) Victim can no longer open a debt position in a new synthetic:
    vm.expectRevert(UserReachedMaxTokens.selector);
    vm.prank(victim);
    msUsdDebtToken.issue(1e18, victim);       // _mint -> addToDebtTokensOfAccount -> revert

    // Liveness only restored after victim fully withdraws one dust token
    // (balance -> 0 -> removeFromDepositTokensOfAccount) — attacker can re-dust.
}
```

### Citations

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

**File:** contracts/DebtToken.sol (L597-600)
```text
        //  Add this token to the debt tokens list if the recipient is receiving it for the 1st time
        if (_balanceBefore == 0 && amount_ > 0) {
            pool.addToDebtTokensOfAccount(account_);
        }
```
