"""Process-local numeric thread defaults, before importing BLAS consumers."""
import os


def configure_numeric_threads(env=None):
    """Respect explicit library settings; 0 disables the service preset entirely."""
    env = os.environ if env is None else env
    count = int(env.get("VECTOR_LAKE_NUMERIC_THREADS", "1"))
    if count < 0 or count > 64:
        raise ValueError("VECTOR_LAKE_NUMERIC_THREADS must be between 0 and 64")
    if count:
        for key in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
            env.setdefault(key, str(count))
    return {key: env.get(key) for key in
            ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS")}
