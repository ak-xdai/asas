"""One module per package, mirroring the host convention Asas expects.

The layout is the answer to the question an adopting host actually asks: *what
is the smallest diff that adds this package to the app I already have?* For any
package here, it is this one file plus one line in the lifespan.

Each module states which contract row(s) it demonstrates in its docstring. A
module that cannot name one does not belong here.
"""
