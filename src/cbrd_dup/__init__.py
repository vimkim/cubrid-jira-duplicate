"""Tools for the CBRD duplicate-issue checker PoC (CBRD-27978)."""

import os

# numpy's OpenBLAS starts a spinning thread per CPU on import: about 10 s of CPU time per command on an
# 80-CPU host, for matrix-vector products that take milliseconds. The embedding model has its own threads.
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
