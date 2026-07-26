Let me analyze the external bug's root cause and search for analogs in the Aptos repository.

The root cause is: **uninitialized cycle/epoch state in a rewards contract allows immediate reward drainage before proper initialization**.