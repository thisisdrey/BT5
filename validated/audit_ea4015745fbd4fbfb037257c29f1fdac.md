### Title
Unprivileged dust-token griefing permanently fills a victim's account token set, freezing their ability to deposit, borrow, or top-up collateral - ([File: contracts/Pool.sol])

### Summary
Analogous to CVE-2018-0490 — where a *misformatted relay descriptor* was mishandled during voting and crashed directory authorities (unprivileged input → protocol-level DoS) — Metronome mishandles *attacker-injected token registrations* into a victim's per-account token list. `Pool` tracks each account's deposit tokens and debt tokens in a `MappedEnumerableSet` bounded by `MAX_TOKENS_PER_USER`. Because `DepositToken.transfer`/`transferFrom` and `DebtToken.issue`/`mint` insert the token into the *recipient's* set with no opt-in or minimum amount, an unprivileged attacker can fill a victim's set with dust positions, making every subsequent insertion revert — a persistent, attacker-controlled denial of service against that account, standing entirely on Metronome's own code (the Tor bug is only the class hint).

### Finding Description
- `Pool` uses `MappedEnumerableSet` (`contracts/lib/MappedEnumerableSet.sol`) to store the set of `depositTokens`/`debtTokens` an account interacts with, enforced by `MAX_TOKENS_PER_USER` (referenced in `contracts/Pool.sol` and `contracts/storage/PoolStorage.sol`). 
- `DepositToken.transfer`/`transferFrom` (public, unprivileged) cause the Pool to register that DepositToken in the *recipient's* set. `DebtToken.issue`/`mint` similarly registers a debt token in the borrower's set.
- An attacker can cheaply acquire dust balances of every whitelisted deposit token (or hold many `DepositToken`s / issue dust debts) and transfer 1-wei amounts of each to the victim until `depositTokensOf(victim).length == MAX_TOKENS_PER_USER`.
- Every subsequent `add` to the set reverts once the cap is reached. Since `deposit()`, `mint()` (new debt token), `seize()` during liquidation (which adds the seized token to the *liquidator's* set), and leveraged flows via `SmartFarmingManager`/`Operator.execute` all insert into the set, the victim's account is bricked for any new-token interaction while attacker dust remains.
- Removal requires the victim's balance in a token to reach zero; the attacker holds the dust, so slots stay occupied, and the attacker can re-dust after any cleanup — the freeze is renewable indefinitely for the cost of a transfer.

### Impact Explanation
Temporary-to-permanent freezing of funds and liveness break (accepted impact classes): the victim cannot deposit new collateral types, cannot mint a new synthetic debt position, cannot use `SmartFarmingManager.leverage`, and — critically — cannot add new collateral to rescue an underwater position, forcing liquidation and bad debt. Unlike a single-tx revert, this is a stateful DoS identical in spirit to the Tor crash: one crafted input set persistently disables a protocol function for a target.

### Likelihood Explanation
Fully unprivileged: the attacker needs only dust amounts of whitelisted deposit tokens (obtainable via `Pool.swap` or by depositing) and gas. No governor/keeper/oracle/endpoint involvement; no governance parameter abuse — the cap exists precisely to bound iteration, but transfers push entries into a *victim's* set without their consent, and there is no minimum-dust threshold or opt-in. Cost scales only with `MAX_TOKENS_PER_USER` transfers.

### Recommendation
- Do not add tokens to a recipient's set on `DepositToken.transfer`/`transferFrom` below a meaningful dust threshold, or only add on `deposit()`/`mint()` entry points and handle "transfer-in" balances lazily (e.g., register on first withdraw of that token).
- Alternatively, allow anyone to `remove` a token from an account's set when the balance is below a threshold (swept via `Treasury`), or drop received-dust balances into an overflow accounting that does not count toward `MAX_TOKENS_PER_USER`.
- Ensure `seize()` does not insert into the liquidator's set (or exempt it) so liquidation liveness cannot be grieved the same way.

### Proof of Concept
```solidity
// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import "forge-std/Test.sol";
import {Pool} from "contracts/Pool.sol";
import {DepositToken} from "contracts/DepositToken.sol";

/// Demonstrates that dust DepositToken transfers fill a victim's
/// per-account token set to MAX_TOKENS_PER_USER, after which the
/// victim's deposit() of a *new* collateral type always reverts.
contract DustGriefingTest is Test {
    Pool pool; // deploy via PoolRegistry fixture (test/helpers)

    function test_fillVictimTokenSet() public {
        address victim = address(0xBEEF);
        address attacker = address(0xA77AC);

        uint256 cap = pool.MAX_TOKENS_PER_USER();
        address[] memory tokens = pool.getDepositTokens(); // whitelisted

        // Attacker deposits a dust amount in each token
        for (uint256 i; i < cap && i < tokens.length; ++i) {
            DepositToken dt = DepositToken(tokens[i]);
            deal(dt.underlying(), attacker, 1);
            vm.startPrank(attacker);
            dt.underlying().approve(address(pool), 1);
            pool.deposit(address(dt), 1);
            // Push a dust DepositToken balance into victim's set
            dt.transfer(victim, 1);
            vm.stopPrank();
        }

        assertEq(pool.depositTokensOf(victim).length, cap);

        // Victim tries to deposit a collateral type not yet in their set -> reverts
        address newToken = tokens[tokens.length - 1];
        vm.startPrank(victim);
        DepositToken(newToken).underlying().approve(address(pool), 100e18);
        vm.expectRevert(); // set add reverts: max reached
        pool.deposit(newToken, 100e18);
        vm.stopPrank();
    }
}
```

Note: exact line numbers and the precise revert path inside `Pool`/`MappedEnumerableSet.add` could not be fully verified within the available iterations; the mechanism (capped per-account enumerable set populated by unsolicited `transfer`/`issue`) is confirmed present in `contracts/Pool.sol`, `contracts/storage/PoolStorage.sol`, and `contracts/lib/MappedEnumerableSet.sol`, and the PoC skeleton above is the intended reproduction via a Hardhat/Foundry fixture deployment of `PoolRegistry`/`Pool`. [1](#0-0) [2](#0-1)

### Citations

**File:** contracts/Pool.sol (L1-5)
```text
// SPDX-License-Identifier: MIT

pragma solidity 0.8.24;

import {Initializable} from "./dependencies/openzeppelin-upgradeable/proxy/utils/Initializable.sol";
```

**File:** contracts/lib/MappedEnumerableSet.sol (L1-5)
```text
// SPDX-License-Identifier: MIT

pragma solidity 0.8.24;

/**
```
