# Golden evidence protection

Golden expected values are independently checked product truth. Never change a golden expectation
merely to make an implementation pass. Fix the implementation instead. If an expectation appears
incorrect, stop, show the original source evidence, and obtain explicit user approval before changing
it. Automated tests must remain offline and use temporary databases and artifact directories.
