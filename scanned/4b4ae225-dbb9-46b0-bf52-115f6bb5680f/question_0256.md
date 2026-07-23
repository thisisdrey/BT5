# Q256: Permanent Lock Path in burn

## Question
Can unprivileged inputs to `aptos_token::token::burn` push the module into a state where assets remain valid but become permanently unclaimable, untransferable, or unrecoverable?

## Target
- File/function: aptos-move/framework/aptos-token/sources/token.move::aptos_token::token::burn
- Entrypoint: Submit an entry-function transaction invoking `aptos_token::token::burn` or the direct user flow that reaches it.
- Attacker controls: collection names, token names, property maps, royalty fields, seeds, creator choices, supply-like amounts, and recipient addresses
- Exploit idea: Find a reachable transition where the module accepts user input yet leaves value stuck behind an impossible future condition.
- Invariant to test: No accepted user transition should strand otherwise-valid assets behind an impossible or permanently unreachable recovery path.
- Expected Immunefi impact: Critical. Irreversible fund lock, permanently unspendable balances, or non-recoverable loss of access to user or protocol value caused by broken execution, storage, object, staking, vesting, multisig, or resource-account flows.
- Fast validation: Add a state-machine test that executes the edge-case transition and then proves a legitimate owner can still recover or move the value.
