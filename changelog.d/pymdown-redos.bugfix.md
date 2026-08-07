Bumped `pymdown-extensions` to 11.0.1, closing GHSA-gm37-52c6-37mw — an
exponential-backtracking ReDoS in the caret, tilde, betterem and magiclink inline
processors. The advisory landed against the pinned 11.0, turning main's CI red
and blocking every open PR on the board behind a dependency none of them touched.
