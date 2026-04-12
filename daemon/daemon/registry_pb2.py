# Re-export from the registry package so that both packages share a single
# descriptor pool registration for registry.proto.  This eliminates the
# "duplicate file name registry.proto" error that arises when the daemon and
# registry are imported in the same process (e.g. integration tests).
from registry.registry_pb2 import *          # noqa: F401, F403
from registry.registry_pb2 import DESCRIPTOR  # noqa: F401
