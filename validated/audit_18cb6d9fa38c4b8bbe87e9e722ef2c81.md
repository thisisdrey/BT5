### Title
Attacker can fill a victim's per-account token list (`MAX_TOKENS_PER_USER`) with dust transfers, reverting all new-collateral and new-debt additions - (File: contracts/Pool.sol)

### Summary
`Pool` keeps two per-account bounded sets (`debtTokensOfAccount`, `depositTokensOfAccount`) whose combined length may never reach `MAX_TOKENS_PER_USER = 30`. Like CVE-2018-16851's single 256MB result buffer that crashes the process when it fills, here the accumulator is a fixed-size per-account list: once it hits the cap, `onlyIfAdditionWillNotReachMaxTokens` reverts every call that would add a new entry — with `UserReachedMaxTokens()`. An unprivileged attacker can fill a victim's list for free (from the victim's perspective) by transferring dust amounts of whitelisted `DepositToken`s to the victim.

### Finding Description
`Pool.addToDepositTokensOfAccount` and `Pool.addToDebtTokensOfAccount` are guarded by `onlyIfAdditionWillNotReachMaxTokens`, which reverts when `debtTokensOfAccount.length(account_) + depositTokensOfAccount.length(account_) >= 30` ( [1](#0-0) [2](#0-1) ).

These functions are invoked from `DepositToken`/`DebtToken` whenever an account's balance goes from 0 to non-zero ( [3](#0-2) ). ERC-20 transfers are permissionless toward the recipient — a `DepositToken.transferFrom`/`transfer` to an arbitrary `account_` triggers `addToDepositTokensOfAccount(account_)` on the victim's behalf. The attacker only needs to hold dust of each deposit token (and can withdraw it back out of the pool afterward, since `DepositToken` shares are freely transferable).

Attack path:
1. Attacker deposits dust into every whitelisted `DepositToken` in the Pool (or acquires shares on the market).
2. Attacker calls `depositToken.transfer(victim, 1 wei)` for N distinct deposit tokens until the victim's combined list length is 30.
3. Every subsequent action by the victim that would introduce a *new* token into their account reverts:
   - `pool.deposit(newDepositToken, ...)` / mint of a new deposit token → `addToDepositTokensOfAccount` reverts.
   - `debtToken.issue(...)` of a synthetic whose debt token the victim doesn't already hold → `addToDebtTokensOfAccount` reverts.
   - `DepositToken.seize` paying out a collateral type the liquidator-receiving account doesn't hold also reverts at the same cap (mitigable by the liquidator using a fresh address).

### Impact Explanation
This is a liveness/position-management DoS on a specific victim:

- **Blocked collateral rescue**: a victim whose position is approaching liquidation cannot deposit a *new* collateral type to restore health (deposit into an already-held token still works — unless the victim holds none or the needed pool asset differs). If the victim's only collateral is crashing and they need to add a different asset, they are forced into liquidation first, causing real loss via the liquidation fee.
- **Blocked debt diversification / swaps**: issuing a new synthetic or receiving proceeds of a `swap` into a synthetic the victim doesn't hold fails.
- **Blocked receipt of funds**: any third party attempting to pay the victim in a deposit token they don't hold causes a revert, temporarily freezing incoming transfers.

The invariant broken is liveness of per-account token registration: a fixed-size accounting buffer is filled by a third party and the protocol has no way to distinguish attacker-forced entries from genuine positions, so legitimate operations revert. The victim can clear slots by transferring the dust away (each transfer to an address not holding the token removes it from their set), but this requires the victim to notice, and the attacker can cheaply re-grief — a persistent, asymmetric denial of position management at the cost of dust deposits.

### Likelihood Explanation
- Attacker model fits: any EOA holding deposit-token shares can push them to any address; no privileged role, oracle manipulation, or governance needed.
- Cost: dust deposits across the whitelisted set (typically far fewer than 30 deposit tokens exist per pool, so combined debt+deposit tokens may be reachable quickly; note each `add` of the *same* token doesn't grow the set, so the attacker is bounded by the number of distinct whitelisted tokens — with few whitelisted tokens the cap may be unreachable on some pools, limiting severity there).
- No modifier stops it: `addToDepositTokensOfAccount` has no opt-in/consent from `account_`, no per-token minimum balance check before registration, and pause flags are irrelevant.
- Persistence is the caveat: the victim can self-heal by sweeping dust out, so this is temporary freezing / griefing rather than permanent lock, consistent with a Medium-severity DoS analog.

### Recommendation
- Skip registration for de-minimis balances: only call `addTo*TokensOfAccount` when the new balance exceeds a dust threshold (e.g., a per-token minimum in USD terms), or prune entries whose balance falls below the threshold.
- Alternatively, drop the hard revert: keep `MAX_TOKENS_PER_USER` but evict/ignore zero-or-dust-balance entries, or raise/remove the cap by iterating only over tokens with non-zero balances tracked in a way that cannot be spam-filled by incoming transfers.
- At minimum, allow accounts to remove entries themselves (a public `removeTokenOfAccount(token)` callable by the account when balance is 0) so recovery is a single cheap call rather than a transfer to a second address.

### Proof of Concept
Foundry fork test sketch (against deployed Pool + whitelisted DepositTokens):

```solidity
function test_dustFillVictimTokenList() public {
    address victim = address(0xV1C);
    address[] memory dTokens = pool.getDepositTokens();

    // Attacker acquires dust of each deposit token and pushes it to the victim
    for (uint256 i; i < dTokens.length && i < 29; ++i) {
        IERC20 underlying = IERC20(IDepositToken(dTokens[i]).underlying());
        deal(address(underlying), attacker, 1e6);
        vm.startPrank(attacker);
        underlying.approve(dTokens[i], 1e6);
        IDepositToken(dTokens[i]).deposit(1);          // attacker mints shares
        IERC20(dTokens[i]).transfer(victim, 1);        // registers token on victim
        vm.stopPrank();
    }
    // Fill remaining slots the same way until length == 30
    assertEq(
        pool.depositTokensOfAccountLength(victim) +
        pool.debtTokensOfAccountLength(victim),
        30
    );

    // Victim cannot deposit a new collateral type
    IDepositToken newToken = IDepositToken(dTokens[dTokens.length - 1]); // one victim doesn't hold
    deal(address(newToken.underlying()), victim, 1e18);
    vm.startPrank(victim);
    newToken.underlying().approve(address(newToken), 1e18);
    vm.expectRevert(UserReachedMaxTokens.selector);
    newToken.deposit(1e18);
    vm.stopPrank();

    // Victim cannot mint a synthetic they don't already owe
    vm.prank(victim);
    vm.expectRevert(UserReachedMaxTokens.selector);
    IDebtToken(newDebtToken).issue(1e18);
}
```

Reproduce: `forge test --fork-url <mainnet/base rpc> --match-test test_dustFillVictimTokenList -vvv`. The reverts originate in `Pool.onlyIfAdditionWillNotReachMaxTokens` (contracts/Pool.sol:143-148) via `DepositToken.transfer`/`deposit` → `addToDepositTokensOfAccount` (contracts/Pool.sol:216-220).

### Citations

**File:** contracts/Pool.sol (L79-79)
```text
    uint256 public constant MAX_TOKENS_PER_USER = 30;
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
