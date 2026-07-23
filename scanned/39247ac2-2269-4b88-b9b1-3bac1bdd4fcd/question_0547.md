# Q547: Asset Accounting Drift in set_beneficiary_for_operator

## Question
Can an unprivileged attacker call `aptos_framework::delegation_pool::set_beneficiary_for_operator` with crafted user inputs and make the module update one balance, supply, owner, or object counter without updating the paired accounting value that should stay in sync?

## Target
- File/function: aptos-move/framework/aptos-framework/sources/delegation_pool.move::aptos_framework::delegation_pool::set_beneficiary_for_operator
- Entrypoint: Submit an entry-function transaction invoking `aptos_framework::delegation_pool::set_beneficiary_for_operator` or the direct user flow that reaches it.
- Attacker controls: stake amounts, lockup windows, delegated voter or operator choices, beneficiaries, commission-related inputs, and recipient addresses
- Exploit idea: Find any path where user-controlled entry-function input breaks coupled accounting for balances, supply, ownership, or share values.
- Invariant to test: Every asset-moving or asset-creating entry function must preserve all paired accounting invariants for amount, owner, and total supply.
- Expected Immunefi impact: Critical. Unauthorized theft, minting, burning, freezing, or reassignment of APT, fungible assets, tokenized assets, staking balances, vesting balances, or other user-controlled on-chain value through transaction validation, Move execution, native-function handling, or state-commitment failure.
- Fast validation: Add a Move or e2e test that calls the entry function with edge-case inputs and asserts supply, owner, and balance invariants before and after execution.
