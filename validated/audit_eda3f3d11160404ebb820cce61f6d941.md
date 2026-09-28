### Title
Unprivileged attacker can fill a victim's per-account token list via dust deposits/transfers, DoS-ing the victim's `deposit`, `issue`, `leverage` and inbound transfers - (File: contracts/Pool.sol)

### Summary
`Pool.addToDepositTokensOfAccount` and `Pool.addToDebtTokensOfAccount` revert with `UserReachedMaxTokens` once the sum of an account's deposit-token and debt-token entries reaches `MAX_TOKENS_PER_USER` (30). Because `DepositToken.deposit(amount_, onBehalfOf_)` lets anyone mint deposit tokens to an arbitrary `onBehalfOf_` address, and `DepositToken.transfer/transferFrom` lets anyone push unlocked deposit tokens to any recipient, an unprivileged attacker can populate a victim's token list with dust entries across all registered deposit tokens. From then on, every code path that adds a *new* token to the victim's list reverts: `deposit` into any collateral the victim doesn't already hold, `DebtToken.issue`/`Pool.swap` minting a debt token the victim doesn't already hold, `SmartFarmingManager.leverage`, and even plain inbound `DepositToken` transfers to the victim.

### Finding Description
In `contracts/Pool.sol`:

```solidity
uint256 public constant MAX_TOKENS_PER_USER = 30;              // line 79

modifier onlyIfAdditionWillNotReachMaxTokens(address account_) {
    if (debtTokensOfAccount.length(account_) + depositTokensOfAccount.length(account_) >= MAX_TOKENS_PER_USER) {
        revert UserReachedMaxTokens();
    }
    _;
}                                                               // lines 143-148

function addToDepositTokensOfAccount(address account_) external onlyIfAdditionWillNotReachMaxTokens(account_) {
    address _depositToken = _msgSender();
    _revertIfSenderIsNotDepositToken(_depositToken);
    if (!depositTokensOfAccount.add(account_, _depositToken)) revert DepositTokenAlreadyExists();
}                                                               // lines 216-220
```

The pool enforces this cap on a combined list (`debtTokensOfAccount` + `depositTokensOfAccount`), and there is no way for the victim or anyone else to remove an entry except by bringing the victim's balance of that specific token to zero (`removeFromDepositTokensOfAccount` is only callable by the token itself, inside `_burn`/`_transfer`).

In `contracts/DepositToken.sol`, two attacker-reachable paths insert entries into the victim's list:

1. `deposit(uint256 amount_, address onBehalfOf_)` (lines 211-237) mints to an arbitrary `onBehalfOf_`; `_mint` calls `pool.addToDepositTokensOfAccount(account_)` whenever the recipient's prior balance was zero (lines 486-488).
2. `transfer`/`transferFrom` (lines 348-376) credit an arbitrary recipient; `_transfer` calls `pool.addToDepositTokensOfAccount(recipient_)` when the recipient's prior balance was zero (lines 518-520).

The attacker can therefore, for each of the pool's registered `DepositToken`s (up to 30 per `addDepositToken`, `Pool.sol` line 703), execute `deposit(1 wei of underlying, victim)` (or a dust `transfer`), filling all 30 slots. Any subsequent attempt by the victim to interact with a token not already in their list reverts:

- `DepositToken.deposit` → `_mint` → `addToDepositTokensOfAccount` → `UserReachedMaxTokens`.
- `DebtToken.issue`/`mint`/`flashIssue` → `addToDebtTokensOfAccount` → `UserReachedMaxTokens` (`contracts/DebtToken.sol` calls the same pool hook).
- `Pool.swap` into a synthetic whose debt token isn't already listed.
- `SmartFarmingManager.leverage`/`flashRepay` flows that mint new debt/deposit tokens to the victim.
- Any inbound `DepositToken.transfer`/`seize` crediting a new token (this also means liquidation `seize` to a liquidator can revert if the liquidator's list is full — griefing liquidations).

There is no pause, guard, or allowlist on `deposit`'s `onBehalfOf_` parameter (`BeneficiaryIsNull` only rejects `address(0)`), and no minimum deposit amount, so the attack costs only dust collateral plus deposit fees. The attacker can repeat it after the victim clears slots, keeping the victim permanently blocked.

### Impact Explanation
Liveness/DoS on a per-account basis, matching the advisory's "low-privileged network attacker causes repeatable denial of service" class:

- The victim cannot deposit *any new collateral type* — `deposit` reverts at mint time after the underlying has already been pulled into the Treasury (the whole tx reverts, so funds are returned, but the operation is unusable).
- The victim cannot `issue` or acquire a debt position in any new synthetic.
- The victim cannot receive a new deposit token via transfer — griefing airdrops, OTC transfers, and third-party integrations sending msd-tokens.
- If the victim has debt and their health deteriorates, a liquidator whose own list is full cannot be paid via `seize` into a new deposit token, and — more importantly — the victim cannot deposit additional collateral of a new type to restore health, forcing avoidable liquidation.
- Recovery requires the victim to spend gas (and `withdrawFee` per token) to zero out up to 30 dust balances; the attacker can refill slots immediately after each cleanup, so this is a sustainable DoS rather than a one-time freeze.

### Likelihood Explanation
- Fully unprivileged: any EOA can call `deposit(amount, victim)` or `transfer(victim, dust)`. No privileged role, oracle manipulation, or governance action needed.
- Cost scales with the number of registered deposit tokens (bounded by `MAX_TOKENS_PER_USER` = 30, enforced in `addDepositToken`) — each slot costs a dust amount of underlying plus deposit fee.
- Deterministic: the revert is a storage-length check, not dependent on market conditions. Counter-mitigation exists (victim clears dust entries at gas+fee cost), which is why this is a repeated-griefing medium-severity DoS rather than a permanent freeze.

### Recommendation
- Reject third-party beneficiary minting or make it opt-in: require `onBehalfOf_ == _msgSender()` in `DepositToken.deposit`, or introduce an account-level `allowDepositsFor` flag the beneficiary must set.
- Decouple the cap from unsolicited receipts: only apply `onlyIfAdditionWillNotReachMaxTokens` to debt tokens (which only the user can take on), and let deposit-token lists grow beyond the cap, or track deposit tokens in a separate list with its own generous bound.
- Alternatively, add a minimum deposit amount and/or skip adding tokens to the list when the minted amount is below a dust threshold, and/or expose a `removeTokenFromMyList(token)` function letting a user purge zero-near balances themselves.

### Proof of Concept
Foundry fork-style reproduction (Hardhat layout equivalent exists in `test/Pool.test.ts` "should revert when reach max tokens"):

```solidity
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

import "forge-std/Test.sol";
import {DepositToken} from "../contracts/DepositToken.sol";
import {Pool} from "../contracts/Pool.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";

contract MaxTokensGriefingTest is Test {
    Pool pool;
    address victim = address(0xVic);
    address attacker = address(0xAtt);

    function test_dustDepositFillsVictimListAndBlocksNewDeposits() public {
        // pool, depositTokens[] and treasury deployed/forked from mainnet
        address[] memory depositTokens = pool.getDepositTokens();

        // 1) Attacker deposits 1 wei of every underlying on behalf of victim
        for (uint256 i; i < depositTokens.length; ++i) {
            DepositToken dt = DepositToken(depositTokens[i]);
            IERC20 underlying = dt.underlying();
            deal(address(underlying), attacker, 1);
            vm.startPrank(attacker);
            underlying.approve(address(dt), 1);
            dt.deposit(1, victim); // mints dust msd-token to victim, adds to victim's list
            vm.stopPrank();
        }

        // victim's list is now full (up to MAX_TOKENS_PER_USER == 30)
        assertEq(
            pool.getDepositTokensOfAccount(victim).length + pool.getDebtTokensOfAccount(victim).length,
            pool.MAX_TOKENS_PER_USER()
        );

        // 2) Victim tries to deposit into a collateral they don't hold yet -> reverts
        // (re-register a fresh DepositToken or use one the attacker skipped)
        DepositToken newDt = DepositToken(newDepositToken); // any token not in victim's list
        IERC20 newUnderlying = newDt.underlying();
        deal(address(newUnderlying), victim, 1 ether);
        vm.startPrank(victim);
        newUnderlying.approve(address(newDt), 1 ether);
        vm.expectRevert(Pool.UserReachedMaxTokens.selector);
        newDt.deposit(1 ether, victim);
        vm.stopPrank();

        // 3) Victim cannot issue a new synthetic debt either
        vm.prank(victim);
        vm.expectRevert(Pool.UserReachedMaxTokens.selector);
        debtToken.issue(1, victim);

        // 4) Even inbound transfers of an unheld deposit token revert
        vm.prank(attacker);
        vm.expectRevert(Pool.UserReachedMaxTokens.selector);
        newDt.transfer(victim, 1);
    }
}
```

Key steps verified against source:
- `deposit` accepts arbitrary `onBehalfOf_` and has no minimum amount (`contracts/DepositToken.sol:211-237`).
- `_mint`/`_transfer` add the token to the recipient's pool list on `0 → >0` balance (`contracts/DepositToken.sol:486-488`, `518-520`).
- The pool reverts once the combined list hits 30 (`contracts/Pool.sol:143-148`, `204-220`).